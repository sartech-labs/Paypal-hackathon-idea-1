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
RANGES = {
    "g01": ("rec_crawler", ("started", -0.3), ("finished", -0.2)),
    "g02": ("rec_crawler", ("finished", -0.2), ("summary", 0.2)),
    "wd1": ("rec_owner", ("overview", -0.55), ("overview_explained", 0.1)),
    "wd2": ("rec_owner", ("overview_explained", 0.7), ("operations", -1.7)),
    "wd3": ("rec_owner", ("operations", -0.4), ("verify_clicked", -0.3)),
    "wd4": ("rec_owner", ("verify_clicked", -0.3), ("verify_result", 5.2)),
    "wd5": ("rec_owner", ("analytics", -1.35), ("end", 0.0)),
    "ud1": ("rec", ("studio_loaded", 1.0), ("run_started", 1.2)),
    "ud2": ("rec", ("call:fetch_page:0", -0.5), ("call:complete_session:0", 1.6)),
    "ud3": ("rec", ("call:buy_access:0", -1.7), ("decision_approved:product", 2.6)),
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
