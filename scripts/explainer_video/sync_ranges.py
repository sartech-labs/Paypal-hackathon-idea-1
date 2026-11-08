"""Point the storyboard's recording scenes at the right moments of fresh recordings.

Each recorder saves marks.json (seconds into its clip for each step). After re-recording,
run this to recompute every recording scene's from/to range from those marks.

Usage:  python scripts/explainer_video/sync_ranges.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent.parent / "video-out"

# scene id -> (clip folder, start = mark + offset, end = mark + offset)
# Every live scene comes from one real-screen take of Chrome on this PC (record_device.py).
RANGES = {
    "web1": ("rec_device", ("website", -0.5), ("for_agents", -1.2)),
    "web2": ("rec_device", ("for_agents", -1.2), ("for_agents", 4.2)),
    "g01": ("rec_device", ("scraper_started", -1.6), ("scraper_finished", -0.2)),
    "g02": ("rec_device", ("scraper_finished", -0.2), ("scraper_verdict", 0.6)),
    "wd1": ("rec_device", ("overview", -1.0), ("overview_explained", 0.1)),
    "wd2": ("rec_device", ("security", -2.2), ("operations", -2.2)),
    "wd3": ("rec_device", ("operations", -2.2), ("verify_clicked", -0.9)),
    "wd4": ("rec_device", ("verify_clicked", -0.9), ("verify_result", 5.2)),
    "wd5": ("rec_device", ("analytics", -1.4), ("analytics", 6.8)),
    "ud1": ("rec_device", ("agent", 0.5), ("run_started", 1.2)),
    "ud2": ("rec_device", ("call:fetch_page:0", -0.5), ("call:buy_access:0", -0.8)),
    "ud3": ("rec_device", ("call:buy_access:0", -0.8), ("decision_declined:subscription", 2.4)),
}


def main() -> None:
    path = HERE / "storyboard.json"
    sb = json.loads(path.read_text(encoding="utf-8"))
    marks = {d: json.loads((OUT / d / "marks.json").read_text()) for d in {r[0] for r in RANGES.values()}}
    for sc in sb["scenes"]:
        spec = RANGES.get(sc["id"])
        if not spec:
            continue
        clip, (a, da), (b, db) = spec
        m = marks[clip]
        if a not in m or b not in m:
            print(f"  {sc['id']}: marks {a!r}/{b!r} missing in {clip}; range kept")
            continue
        sc["visual"]["from"], sc["visual"]["to"] = round(m[a] + da, 2), round(m[b] + db, 2)
        print(f"  {sc['id']}: {sc['visual']['from']}-{sc['visual']['to']}s of {clip}")
    path.write_text(json.dumps(sb, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
