"""Build the SARA explainer video (1920x1080, 30 fps).

Steps (each cached under video-out/build/):
  voice    OpenAI text-to-speech per scene (wav), then Whisper word timestamps
  frames   recording scenes: frames cut from the screen recordings, sped to fit the voice
  render   every frame drawn by engine.html in Chromium and piped to ffmpeg (H.264)
  mux      narration track (with -14 LUFS loudness) + video -> sara-explainer.mp4, .srt

Usage:  python scripts/explainer_video/build.py [--only-preview s03,d03] [--force-voice]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import threading
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import imageio_ffmpeg
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUT = ROOT / "video-out"
BUILD = OUT / "build"
FPS = 30
LEAD = 0.12   # silence before each line
GAP = 0.30    # silence after each line
TEMPO = 1.07  # slightly brisker delivery (pitch preserved)
FF = imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


# --- voice ---------------------------------------------------------------------------------

def make_voice(sb: dict, force: bool) -> None:
    import openai

    client = openai.OpenAI()
    adir = BUILD / "audio"
    adir.mkdir(parents=True, exist_ok=True)
    for sc in sb["scenes"]:
        key = hashlib.sha1(json.dumps([sc["vo"], sb["voice"], sb["instructions"], TEMPO]).encode()).hexdigest()[:12]
        wav_path = adir / f"{sc['id']}.wav"
        meta_path = adir / f"{sc['id']}.json"
        if not force and meta_path.exists() and json.loads(meta_path.read_text())["key"] == key and wav_path.exists():
            continue
        print(f"  voice {sc['id']}: {sc['vo'][:60]}...", flush=True)
        expected = len(sc["vo"].split())
        for attempt in range(3):
            words = _speak(client, sb, sc, wav_path)
            if len(words) >= 0.8 * expected:
                break
            print(f"    only {len(words)} of {expected} words heard; regenerating", flush=True)
        else:
            print(f"    WARNING {sc['id']}: narration still looks incomplete", flush=True)
        meta_path.write_text(json.dumps({"key": key, "duration": wav_duration(wav_path), "words": words}, indent=1))


def _transcribe(client, wav_path: Path, prompt: str | None) -> list[dict]:
    with open(wav_path, "rb") as fh:
        kw = {"prompt": prompt} if prompt else {}
        tr = client.audio.transcriptions.create(model="whisper-1", file=fh, response_format="verbose_json", timestamp_granularities=["word"], **kw)
    return [{"w": w.word, "start": round(w.start, 3), "end": round(w.end, 3)} for w in (tr.words or [])]


def _speak(client, sb: dict, sc: dict, wav_path: Path) -> list[dict]:
    """One TTS take of the scene, trimmed and sped up, then word timestamps from Whisper."""
    import openai

    for model in ("gpt-4o-mini-tts", "tts-1-hd"):
        try:
            kwargs = dict(model=model, voice=sb["voice"] if model != "tts-1-hd" else "nova", input=sc["vo"], response_format="wav")
            if model == "gpt-4o-mini-tts":
                kwargs["instructions"] = sb["instructions"]
            resp = client.audio.speech.create(**kwargs)
            raw = OUT / "build" / "audio" / f"{sc['id']}.raw.wav"
            raw.write_bytes(resp.content)
            break
        except openai.APIError as exc:
            print(f"    {model} failed: {exc}", flush=True)
    else:
        sys.exit("TTS failed")
    # Normalize to 24 kHz mono 16-bit and trim leading/trailing silence a little.
    run([FF, "-y", "-v", "error", "-i", str(raw), "-af", f"silenceremove=start_periods=1:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse,atempo={TEMPO}", "-ar", "24000", "-ac", "1", "-sample_fmt", "s16", str(wav_path)])
    raw.unlink()
    # Whisper with the script as a prompt spells names right but sometimes skips a sentence;
    # keep whichever transcript heard more words.
    words = _transcribe(client, wav_path, sc["vo"])
    if len(words) < len(sc["vo"].split()):
        plain = _transcribe(client, wav_path, None)
        if len(plain) > len(words):
            words = plain
    return words


def timeline(sb: dict) -> list[dict]:
    t = 0.0
    out = []
    for sc in sb["scenes"]:
        meta = json.loads((BUILD / "audio" / f"{sc['id']}.json").read_text())
        dur = LEAD + meta["duration"] + GAP + float(sc.get("hold", 0))
        words = [{"w": w["w"], "start": round(w["start"] + LEAD, 3)} for w in meta["words"]]
        out.append({"id": sc["id"], "start": t, "dur": dur, "frames": round(dur * FPS), "words": words, "audio": meta["duration"]})
        t += dur
    return out


# --- recording frames ---------------------------------------------------------------------

def cut_frames(sb: dict, tl: list[dict]) -> None:
    for sc, info in zip(sb["scenes"], tl):
        v = sc["visual"]
        if v.get("type") != "rec":
            continue
        fdir = BUILD / "rec" / sc["id"]
        speed = (v["to"] - v["from"]) / info["dur"]
        v["speed"] = speed
        stamp = fdir / "spec.json"
        # Real-screen clips (the whole browser window) are shown larger than app-only recordings.
        size = "1440:774" if v.get("device") else "1340:754"
        spec = {"from": v["from"], "to": v["to"], "dur": info["dur"], "src": v["src"], "size": size}
        if stamp.exists() and json.loads(stamp.read_text()) == spec:
            continue
        shutil.rmtree(fdir, ignore_errors=True)
        fdir.mkdir(parents=True)
        print(f"  frames {sc['id']}: {v['from']}-{v['to']}s at {speed:.2f}x", flush=True)
        run([FF, "-y", "-v", "error", "-ss", str(v["from"]), "-to", str(v["to"]), "-i", str(OUT / v["src"]),
             "-vf", f"setpts=(PTS-STARTPTS)/{speed:.5f},fps={FPS},scale={size}:flags=lanczos", "-q:v", "3", str(fdir / "%05d.jpg")])
        stamp.write_text(json.dumps(spec))


# --- render ------------------------------------------------------------------------------

def render(sb: dict, tl: list[dict], only: list[str] | None) -> Path:
    from playwright.sync_api import sync_playwright

    data = json.loads(json.dumps(sb))
    for sc, info in zip(data["scenes"], tl):
        sc["words"] = info["words"]
        sc["lead"] = LEAD
        sc["dur"] = info["frames"] / FPS
    preview_dir = OUT / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    video_path = BUILD / "video.mp4"
    total = sum(i["frames"] for i in tl)
    # Serve the repo over loopback HTTP: Chromium will not load file:// frames reliably.
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D401
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(ROOT)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"

    def url_for(path: Path) -> str:
        return base_url + "/" + path.resolve().relative_to(ROOT).as_posix()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        page.goto(url_for(HERE / "engine.html"))
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(1500)
        page.evaluate("(sb) => window.__load(sb)", data)
        page.evaluate("document.fonts.ready")
        (BUILD / "events.json").write_text(json.dumps(page.evaluate("window.__events()")), encoding="utf-8")

        def frame_src(sc: dict, f: int) -> str | None:
            if sc["visual"].get("type") != "rec":
                return None
            fdir = BUILD / "rec" / sc["id"]
            files = sorted(fdir.glob("*.jpg"))
            if not files:
                return None
            return url_for(files[min(f, len(files) - 1)])

        if only:
            for i, (sc, info) in enumerate(zip(data["scenes"], tl)):
                if sc["id"] not in only:
                    continue
                for frac in (0.04, 0.35, 0.92):
                    f = int(info["frames"] * frac)
                    page.evaluate("([i, t, src]) => window.__render(i, t, src)", [i, f / FPS, frame_src(sc, f)])
                    page.screenshot(path=str(preview_dir / f"{sc['id']}_{int(frac*100)}.jpg"), type="jpeg", quality=85)
            browser.close()
            httpd.shutdown()
            return preview_dir

        enc = subprocess.Popen([FF, "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
                                "-vf", "scale=in_range=pc:out_range=tv,format=yuv420p", "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart", str(video_path)],
                               stdin=subprocess.PIPE)
        done = 0
        t0 = time.time()
        for i, (sc, info) in enumerate(zip(data["scenes"], tl)):
            for f in range(info["frames"]):
                page.evaluate("([i, t, src]) => window.__render(i, t, src)", [i, f / FPS, frame_src(sc, f)])
                enc.stdin.write(page.screenshot(type="jpeg", quality=92))
                done += 1
            el = time.time() - t0
            print(f"  rendered {sc['id']}  {done}/{total} frames  {el:.0f}s", flush=True)
        enc.stdin.close()
        enc.wait()
        browser.close()
    httpd.shutdown()
    return video_path


# --- sound design (synthesized: no third-party audio) -------------------------------------

SR = 48000


def _write_wav(path: Path, x) -> Path:
    import numpy as np

    x = np.clip(x, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return path


def _sfx_bank() -> dict:
    import numpy as np

    rng = np.random.default_rng(7)

    def t(d):
        return np.arange(int(d * SR)) / SR

    def smooth(x, n):
        k = np.ones(n) / n
        return np.convolve(x, k, mode="same")

    tt = t(0.07)
    tick = 0.22 * np.sin(2 * np.pi * (1500 - 4000 * tt) * tt) * np.exp(-tt * 70)
    tt = t(0.35)
    thud = (0.6 * np.sin(2 * np.pi * 85 * tt) * np.exp(-tt * 14)
            + 0.25 * smooth(rng.standard_normal(tt.size), 40) * np.exp(-tt * 30))
    tt = t(1.6)
    boom = (0.7 * np.sin(2 * np.pi * (58 - 18 * tt) * tt) * np.exp(-tt * 3.2)
            + 0.18 * smooth(rng.standard_normal(tt.size), 25) * np.exp(-tt * 6))
    tt = t(0.55)
    env = np.sin(np.pi * np.clip(tt / 0.55, 0, 1)) ** 2
    noise = rng.standard_normal(tt.size)
    whoosh = 0.32 * (smooth(noise, 9) - smooth(noise, 60)) * env
    tt = t(0.9)
    env = (tt / 0.9) ** 2 * np.exp(-np.clip(tt - 0.8, 0, None) * 30)
    riser = 0.22 * (smooth(rng.standard_normal(tt.size), 6) * env + 0.5 * np.sin(2 * np.pi * (180 + 500 * tt) * tt) * env)
    return {"tick": tick, "thud": thud, "impact": boom, "whoosh": whoosh, "riser": riser}


def make_sfx(sb: dict, tl: list[dict], total_s: float) -> Path:
    import numpy as np

    events = json.loads((BUILD / "events.json").read_text())
    bank = _sfx_bank()
    out = np.zeros(int(total_s * SR) + SR)
    last_tick = -1.0
    offset = {"whoosh": -0.12, "riser": -0.75}
    for evs, info in zip(events, tl):
        for e in sorted(evs, key=lambda e: e["t"]):
            g = info["start"] + e["t"] + offset.get(e["k"], 0)
            if e["k"] == "tick":
                if g - last_tick < 0.11:
                    continue
                last_tick = g
            s = bank[e["k"]]
            i = max(0, int(g * SR))
            out[i:i + s.size] += s[: out.size - i]
    return _write_wav(BUILD / "sfx.wav", out * 0.8)


NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _chord(name: str) -> list[float]:
    root = NOTE[name[0]]
    third = 3 if name.endswith("m") else 4
    base = 220 * 2 ** ((root - 9) / 12)          # octave around A3
    return [base * 2 ** (s / 12) for s in (0, third, 7, 12)]


def make_music(sb: dict, tl: list[dict], total_s: float) -> Path:
    """A quiet pad: tense minor chords for the problem, brighter major ones from the turn onwards."""
    import numpy as np

    n = int(total_s * SR) + SR
    out = np.zeros(n)
    turn = next((i["start"] for sc, i in zip(sb["scenes"], tl) if sc.get("act", 0) >= 1), total_s)
    tense, bright = ["Am", "F", "Dm", "E"], ["C", "G", "Am", "F"]
    bar = 3.2
    t0 = 0.0
    k = 0
    while t0 < total_s:
        prog = tense if t0 < turn else bright
        freqs = _chord(prog[k % 4])
        d = bar + 1.2
        tt = np.arange(int(d * SR)) / SR
        env = np.minimum(1, tt / 0.9) * np.minimum(1, np.clip(d - tt, 0, None) / 1.2)
        seg = np.zeros(tt.size)
        for f in freqs:
            seg += np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(2 * np.pi * 2.003 * f * tt) + 0.12 * np.sin(2 * np.pi * 0.5 * f * tt)
        seg += 0.8 * np.sin(2 * np.pi * freqs[0] / 2 * tt)                     # bass
        if t0 >= turn:                                                           # soft plucked pulse in the solution
            for j in range(int(bar / 0.4)):
                p0 = int(j * 0.4 * SR)
                pt = tt[: int(0.4 * SR)]
                f = freqs[(j * 2) % 4] * 2
                pl = 0.6 * np.sin(2 * np.pi * f * pt) * np.exp(-pt * 9)
                seg[p0:p0 + pl.size] += pl[: seg.size - p0]
        i = int(t0 * SR)
        m = min(seg.size, n - i)
        out[i:i + m] += (seg * env)[:m]
        t0 += bar
        k = 0 if (t0 >= turn and t0 - bar < turn) else k + 1
    # fade in/out, then a modest level so it sits well under the voice
    fade = int(2 * SR)
    out[:fade] *= np.linspace(0, 1, fade)
    end = int(total_s * SR)
    out[end - 3 * SR:end] *= np.linspace(1, 0, 3 * SR)
    out[end:] = 0
    out *= 0.09 / (np.max(np.abs(out)) + 1e-9)
    return _write_wav(BUILD / "music.wav", out)


# --- audio + mux --------------------------------------------------------------------------

def mux(sb: dict, tl: list[dict], video: Path) -> Path:
    rate = 24000
    track = BUILD / "voice.wav"
    with wave.open(str(track), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        for sc, info in zip(sb["scenes"], tl):
            with wave.open(str(BUILD / "audio" / f"{sc['id']}.wav")) as w:
                pcm = w.readframes(w.getnframes())
            lead = int(LEAD * rate) * 2
            total = int(round(info["frames"] / FPS * rate)) * 2
            body = b"\x00" * lead + pcm
            body = body[:total] + b"\x00" * max(0, total - len(body))
            out.writeframes(body)
    final = OUT / sb.get("output", "sara-explainer.mp4")
    total_s = sum(i["frames"] for i in tl) / FPS
    sfx_path, music_path = make_sfx(sb, tl, total_s), make_music(sb, tl, total_s)
    # Voice on top; music ducks under the voice (sidechain); effects mixed in; then loudness to -14 LUFS.
    mix = ("[1:a]aresample=48000,asplit=2[v][vsc];"
           "[2:a][vsc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350:makeup=1[md];"
           "[v][md][3:a]amix=inputs=3:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    run([FF, "-y", "-v", "error", "-i", str(video), "-i", str(track), "-i", str(music_path), "-i", str(sfx_path),
         "-filter_complex", mix, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
         "-ar", "48000", "-c:a", "aac", "-b:a", "192k", "-shortest", str(final)])
    # SRT from the caption pages (by spoken words).
    lines = []
    n = 1
    for sc, info in zip(sb["scenes"], tl):
        text = re.sub(r"\*", "", sc.get("caption") if sc.get("caption") is not None else sc["vo"]).replace(" / ", " ")
        if not text.strip():
            text = sc["vo"]
        a = info["start"] + LEAD
        b = info["start"] + LEAD + info["audio"] + 0.2

        def ts(x: float) -> str:
            ms = int(round(x * 1000))
            return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

        lines.append(f"{n}\n{ts(a)} --> {ts(b)}\n{text}\n")
        n += 1
    (OUT / sb.get("output", "sara-explainer.mp4")).with_suffix(".srt").write_text("\n".join(lines), encoding="utf-8")
    return final


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-preview", default="")
    ap.add_argument("--force-voice", action="store_true")
    ap.add_argument("--skip-voice", action="store_true")
    args = ap.parse_args()
    sb = json.loads((HERE / "storyboard.json").read_text(encoding="utf-8"))
    BUILD.mkdir(parents=True, exist_ok=True)
    if not args.skip_voice:
        make_voice(sb, args.force_voice)
    tl = timeline(sb)
    total = sum(i["dur"] for i in tl)
    print(f"timeline: {len(tl)} scenes, {total:.1f}s")
    for i in tl:
        print(f"  {i['id']}  {i['dur']:5.2f}s  words={len(i['words'])}")
    cut_frames(sb, tl)
    only = [s for s in args.only_preview.split(",") if s]
    out = render(sb, tl, only or None)
    if only:
        print("previews in", out)
        return
    final = mux(sb, tl, out)
    print("done:", final)


if __name__ == "__main__":
    main()
