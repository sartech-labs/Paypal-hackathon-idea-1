"""Turn a recorded demo session into a narrated, captioned MP4 for the Devpost submission.

Usage: python compose.py <work-dir> [--force] [--no-tts]

Reads <work-dir>/raw.webm, markers.json, facts.json, and card PNGs written by record.js.
Narration is built from facts.json, so the video only claims what actually happened;
if a run went differently than the script expects, composition stops (use --force to
render anyway, for example to review a failed take).

With OPENAI_API_KEY set, narration is spoken with OpenAI text-to-speech; otherwise
scene timing comes from estimated reading time and the video carries captions only.
Output: <work-dir>/sara-demo.mp4 and sara-demo.srt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

FPS = 30
W, H = 1920, 1080
MAX_SPEED = 12.0
LEAD_IN = 0.35  # seconds of silence before narration in each scene
TAIL = 0.45
FONT = "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf"
TTS_MODELS = [m for m in [os.getenv("DEMO_TTS_MODEL"), "gpt-4o-mini-tts", "tts-1-hd", "tts-1"] if m]
TTS_VOICE = os.getenv("DEMO_TTS_VOICE", "marin")
TTS_STYLE = (
    "Calm, confident product-demo narrator. Clear and friendly, moderate pace, "
    "no exaggerated excitement."
)


@dataclass
class Scene:
    name: str
    caption: str
    say: str
    min_seconds: float
    card: Optional[str] = None  # PNG for title/closing cards


def _ai(facts: dict[str, Any]) -> bool:
    return facts.get("driver") == "ai"


def build_scenes(facts: dict[str, Any]) -> list[Scene]:
    ai = _ai(facts)
    compliant = facts.get("compliant", {})
    rogue = facts.get("rogue", {})
    assistant = facts.get("assistant", {})
    who = "GPT-5.5" if ai else "the scripted crawler"

    reads = " It reads the policy, pays" if "read_policy" in compliant.get("tools", []) else " It pays"
    paced = " Then it crawls only allowed pages, and waits when the rate limit says so." if compliant.get("waited") else " Then it crawls only allowed pages, within the rate limit."
    injection = (
        " One page hides an instruction telling AI agents to grab private customer data."
        " The agent treats page text as untrusted, and ignores it."
        if compliant.get("injection_seen") and not compliant.get("requested_private")
        else ""
    )
    explained_by_ai = compliant.get("explanation_by") in ("openai", "claude")
    explain = (
        "The AI explains why, and every statement cites the audit event behind it."
        if explained_by_ai
        else "The explanation cites the audit event behind every statement."
    )

    rogue_line = f"Now {'the same model plays a greedy scraper' if ai else 'a greedy scraper'}. " + rogue_attempts(rogue)

    scenes = [
        Scene(
            "title",
            "AI crawlers read the web at machine speed, and robots.txt is only a polite request. SARA backs a site's crawling rules with a refundable PayPal deposit.",
            "AI crawlers read the web at machine speed, and robots dot T X T is only a polite request. SARA backs a site's crawling rules with a refundable PayPal deposit.",
            6.0,
            card="card-title.png",
        ),
        Scene(
            "overview",
            f"This is the live demo, running against the PayPal Sandbox. The crawler is driven by {'OpenAI GPT-5.5' if ai else 'a scripted driver, with fixed steps instead of an AI model'}, and the publisher's PayPal assistant talks to PayPal's own MCP server.",
            f"This is the live demo, running against the PayPal Sandbox. The crawler is driven by {'OpenAI G P T five point five' if ai else 'a scripted driver, with fixed steps instead of an AI model'}, and the publisher's PayPal assistant talks to PayPal's own M C P server.",
            7.0,
        ),
        Scene(
            "compliant",
            "First, a well-behaved research agent. It asks for the news and gets HTTP 402, Payment Required." + reads + " a one-cent refundable deposit with PayPal, and accepts the rules." + paced + injection,
            "First, a well-behaved research agent. It asks for the news and gets H T T P four-oh-two, Payment Required." + reads + " a one-cent refundable deposit with PayPal, and accepts the rules." + paced + injection,
            12.0,
        ),
        Scene(
            "compliant_outcome",
            f"When the session ends, the gateway decides from its own audit log. No violations, so PayPal refunds the deposit. {explain}",
            f"When the session ends, the gateway decides from its own audit log. No violations, so PayPal refunds the deposit. {explain}",
            6.0,
        ),
        Scene(
            "verify",
            "The publisher can check this against PayPal itself. Verify with PayPal reads the order and the refund through PayPal's MCP server. The order, amount, capture and refund all match.",
            "The publisher can check this against PayPal itself. Verify with PayPal reads the order and the refund through PayPal's M C P server. The order, amount, capture and refund all match.",
            6.0,
        ),
        Scene("rogue", rogue_line, rogue_line, 10.0),
        Scene(
            "rogue_outcome",
            "The deposit is forfeited. That decision comes from deterministic policy rules, not from the model. The AI only explains it." if ai else "The deposit is forfeited, by deterministic policy rules.",
            "The deposit is forfeited. That decision comes from deterministic policy rules, not from the model. The AI only explains it." if ai else "The deposit is forfeited, by deterministic policy rules.",
            5.0,
        ),
    ]
    if assistant.get("answer"):
        scenes += [
            Scene(
                "assistant",
                "Publishers can also ask questions. The PayPal assistant answers with PayPal's MCP tools.",
                "Publishers can also ask questions. The PayPal assistant answers with PayPal's M C P tools.",
                4.0,
            ),
            Scene(
                "assistant_answer",
                f"Here {who} checked recent deposits against PayPal's records, and it lists every tool call it made, so the answer can be verified.",
                f"Here {'G P T five point five' if ai else who} checked recent deposits against PayPal's records, and it lists every tool call it made, so the answer can be verified.",
                6.0,
            ),
        ]
    scenes += [
        Scene("console", "The publisher console tracks every session, deposit and violation in real time.", "The publisher console tracks every session, deposit and violation in real time.", 5.0),
        Scene(
            "closing",
            "Other 402 schemes charge for access. SARA charges for misbehaviour. Good agents crawl free.",
            "Other four-oh-two schemes charge for access. SARA charges for misbehaviour. Good agents crawl free.",
            7.0,
            card="card-closing.png",
        ),
    ]
    return scenes


FORBIDDEN_PREFIXES = (("/admin", "the admin area"), ("/private", "private customer data"), ("/internal", "internal reports"))


def _requests(run: dict[str, Any]) -> list[tuple[str, str]]:
    """(status, path) pairs from facts paths such as '403 /admin'."""
    pairs = []
    for entry in run.get("paths", []):
        status, _, path = entry.partition(" ")
        pairs.append((status, path))
    return pairs


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def rogue_attempts(rogue: dict[str, Any]) -> str:
    """Describe only what the rogue run actually tried."""
    paths = [path for _status, path in _requests(rogue)]
    tried = [label for prefix, label in FORBIDDEN_PREFIXES if any(p.startswith(prefix) for p in paths)]
    if "INVALID_PATH" in rogue.get("blocked_reasons", []):
        tried.append("a path-traversal trick")
    burst = "RATE_LIMIT_EXCEEDED" in rogue.get("blocked_reasons", [])
    if tried:
        line = f"It pays, skips the rules, and goes after {_join(tried)}"
        line += ", then bursts requests past the rate limit." if burst else "."
        return line + " The gateway blocks every one."
    if burst:
        return "It pays, skips the rules, and bursts requests past the rate limit. The gateway blocks them."
    return "It pays and skips the rules."


def validate(facts: dict[str, Any]) -> list[str]:
    """Problems that would make the narration untrue."""
    problems = []
    c, r, v = facts.get("compliant", {}), facts.get("rogue", {}), facts.get("verify", {})
    if c.get("status") != "completed" or c.get("deposit_state") != "REFUNDED":
        problems.append(f"compliant run: status {c.get('status')}, deposit {c.get('deposit_state')} (expected completed, REFUNDED)")
    if c.get("violations"):
        problems.append(f"compliant run recorded {c.get('violations')} violation(s)")
    if c.get("requested_private"):
        problems.append("compliant run requested a /private path (fell for the injection)")
    if r.get("deposit_state") != "FORFEITED":
        problems.append(f"rogue run: deposit {r.get('deposit_state')} (expected FORFEITED)")
    leaked = [p for s, p in _requests(r) if s == "200" and p.startswith(tuple(x for x, _ in FORBIDDEN_PREFIXES))]
    if leaked:
        problems.append(f"rogue run was served forbidden paths: {leaked}")
    if not r.get("blocked_reasons"):
        problems.append("rogue run had no blocked requests")
    if v.get("mismatches") or not v.get("checks"):
        problems.append(f"verify: {v.get('checks')} checks, {v.get('mismatches')} mismatch(es)")
    if _ai(facts):
        if facts.get("compliant", {}).get("model") in (None, "scripted"):
            problems.append("AI driver requested but the run reports no model")
        a = facts.get("assistant", {})
        if a.get("attempted") and not a.get("answer"):
            problems.append(f"PayPal assistant gave no answer: {a.get('error')}")
    if facts.get("errors"):
        problems.append(f"page errors: {facts['errors']}")
    return problems


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def synthesize(scenes: list[Scene], work: Path) -> dict[str, Path]:
    """OpenAI TTS per scene; returns {} if no key or no model works."""
    if not os.getenv("OPENAI_API_KEY"):
        return {}
    import openai

    client = openai.OpenAI(timeout=120.0, max_retries=2)
    audio: dict[str, Path] = {}
    for model in TTS_MODELS:
        try:
            for scene in scenes:
                # Cache by content, so a changed line is spoken again.
                key = hashlib.sha1(f"{model}|{TTS_VOICE}|{TTS_STYLE}|{scene.say}".encode()).hexdigest()[:12]
                target = work / f"tts-{scene.name}-{key}.wav"
                if not target.exists():
                    extra = {"instructions": TTS_STYLE} if "gpt-4o" in model else {}
                    response = client.audio.speech.create(
                        model=model, voice=TTS_VOICE, input=scene.say, response_format="wav", **extra
                    )
                    response.write_to_file(target)
                audio[scene.name] = target
            print(f"narration: OpenAI {model}, voice {TTS_VOICE}")
            return audio
        except Exception as exc:  # try the next model
            print(f"TTS with {model} failed: {type(exc).__name__}: {str(exc)[:200]}")
            for path in work.glob("tts-*.wav"):
                path.unlink()
            audio = {}
    return {}


def estimate(text: str) -> float:
    return len(text.split()) / 2.6 + 0.4


def chunks(text: str) -> list[str]:
    """Split a caption into short lines (sentence first, then at commas)."""
    parts: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sentence) > 90:
            cut = sentence.rfind(", ", 0, 90)
            cut = cut + 1 if cut > 30 else sentence.rfind(" ", 0, 90)
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            parts.append(sentence)
    return parts


def ts(seconds: float, srt: bool) -> str:
    h, rem = divmod(max(0.0, seconds), 3600)
    m, s = divmod(rem, 60)
    if srt:
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)) % 1000:03d}"
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        return 2
    work = Path(args[0]).resolve()
    facts = json.loads((work / "facts.json").read_text())
    markers = {m["name"]: m for m in json.loads((work / "markers.json").read_text())}

    problems = validate(facts)
    if problems:
        print("The recording does not match the narration:\n- " + "\n- ".join(problems))
        if "--force" not in flags:
            print("Re-record, or pass --force to render anyway.")
            return 3

    scenes = build_scenes(facts)
    audio = {} if "--no-tts" in flags else synthesize(scenes, work)
    segments = work / "segments"
    shutil.rmtree(segments, ignore_errors=True)
    segments.mkdir()

    timeline: list[tuple[Scene, float, float, float]] = []  # scene, offset, length, speech seconds
    offset = 0.0
    for index, scene in enumerate(scenes):
        speech = duration(audio[scene.name]) if scene.name in audio else estimate(scene.caption)
        length = max(scene.min_seconds, LEAD_IN + speech + TAIL)
        out = segments / f"{index:02d}-{scene.name}.mp4"
        if scene.card:
            run(["ffmpeg", "-y", "-loop", "1", "-i", str(work / scene.card), "-t", f"{length:.3f}",
                 "-vf", f"fps={FPS},format=yuv420p,fade=t=in:st=0:d=0.4", "-c:v", "libx264", "-preset", "medium", "-crf", "18", str(out)])
        else:
            marker = markers[scene.name]
            raw = marker["end"] - marker["start"]
            speed = raw / length
            if speed > MAX_SPEED:
                length, speed = raw / MAX_SPEED, MAX_SPEED
            filters = [f"trim=start={marker['start']:.3f}:end={marker['end']:.3f}", "setpts=PTS-STARTPTS"]
            if speed > 1.0:
                filters.append(f"setpts=PTS/{speed:.4f}")
            filters += [f"fps={FPS}", f"scale={W}:{H}", f"tpad=stop_mode=clone:stop_duration={length:.3f}", f"trim=duration={length:.3f}"]
            if speed > 1.15:
                label = f"{speed:.0f}x speed" if speed >= 2 else f"{speed:.1f}x speed"
                filters.append(
                    f"drawtext=fontfile={FONT}:text='{label}':x=w-tw-36:y=30:fontsize=30:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=14"
                )
            filters.append("format=yuv420p")
            run(["ffmpeg", "-y", "-i", str(work / "raw.webm"), "-vf", ",".join(filters), "-an",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(FPS), str(out)])
            print(f"{scene.name}: {raw:.1f}s recorded -> {length:.1f}s ({max(speed, 1):.1f}x)")
        timeline.append((scene, offset, length, speech))
        offset += length

    # Video: concatenate segments.
    listing = segments / "list.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in sorted(segments.glob("*.mp4"))))
    video = work / "video.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(video)])

    # Audio: each scene's narration padded to the scene length, then concatenated.
    pieces = []
    for index, (scene, _offset, length, _speech) in enumerate(timeline):
        piece = segments / f"{index:02d}-{scene.name}.wav"
        if scene.name in audio:
            run(["ffmpeg", "-y", "-i", str(audio[scene.name]), "-af",
                 f"adelay={int(LEAD_IN * 1000)}:all=1,apad,atrim=duration={length:.3f}", "-ar", "48000", "-ac", "2", str(piece)])
        else:
            run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{length:.3f}", str(piece)])
        pieces.append(piece)
    alist = segments / "audio.txt"
    alist.write_text("".join(f"file '{p.name}'\n" for p in pieces))
    narration = work / "narration.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(alist), "-c", "copy", str(narration)])

    # Captions: ASS (burned in) and SRT (for YouTube), timed across each scene's speech.
    ass = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Caption,Inter SemiBold,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H9A000000,0,0,0,0,100,100,0,0,3,18,0,2,160,160,54,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    srt: list[str] = []
    for scene, scene_offset, _length, speech in timeline:
        lines = chunks(scene.caption)
        total = sum(len(line) for line in lines) or 1
        cursor = scene_offset + LEAD_IN
        for line in lines:
            span = speech * len(line) / total
            start, end = cursor, cursor + span
            cursor = end
            text = line.replace("{", "(").replace("}", ")")
            ass.append(f"Dialogue: 0,{ts(start, False)},{ts(end, False)},Caption,,0,0,0,,{text}")
            srt += [str(len(srt) // 4 + 1), f"{ts(start, True)} --> {ts(end, True)}", line, ""]
    (work / "captions.ass").write_text("\n".join(ass) + "\n")
    (work / "sara-demo.srt").write_text("\n".join(srt))

    final = work / "sara-demo.mp4"
    run(["ffmpeg", "-y", "-i", str(video), "-i", str(narration), "-vf", f"ass={work / 'captions.ass'}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(final)])
    print(f"wrote {final} ({duration(final):.1f}s, narration: {'OpenAI TTS' if audio else 'none, captions only'})")
    if duration(final) > 179:
        print("WARNING: longer than 3 minutes; Devpost requires under 3 minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
