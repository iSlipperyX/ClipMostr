"""TikTok Manager's importer, run on the clips this version made.

TikTok Manager (the TTUpdates repo) watches ClipMonster's folder and imports each finished clip
with what clips.json says about it. This runs its real importer (Store.add_paths, the way a watched
folder imports) and its Ready page count (watch_report) on ClipMonster output folders, in a
throwaway profile, and checks that:

* every clip clips.json lists as ready is imported once, with its title, and no rejected clip is;
* nothing from the download cache or a _rejected folder comes in;
* clip request folders (Requests/<request>/...) and series parts import like any other;
* the Ready page counts agree with what was imported.

    python tests/tiktok_manager/importer_check.py TTUPDATES_DIR CLIPS_DIR [CLIPS_DIR ...]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    tm, folders = Path(sys.argv[1]).resolve(), [Path(a).resolve() for a in sys.argv[2:]]
    profile = Path(tempfile.mkdtemp(prefix="tm-check-"))
    os.environ["APPDATA"] = str(profile / "roaming")
    os.environ["LOCALAPPDATA"] = str(profile / "local")
    sys.path.insert(0, str(tm))
    from tokmanager.ready import watch_report
    from tokmanager.store import Store

    problems: list[str] = []
    expected: dict[str, dict] = {}
    rejected: set[str] = set()
    requests = parts = 0
    for root in folders:
        for index in root.rglob("clips.json"):
            if ".downloads" in index.parts:
                continue
            data = json.loads(index.read_text(encoding="utf-8"))
            for c in data["clips"]:
                path = str((index.parent / c["file"]).resolve())
                if c["file"].startswith("_rejected/") or (c.get("qc") or {}).get("ok") is False:
                    rejected.add(path)
                else:
                    expected[path] = c
                    requests += "Requests" in index.parts
                    parts += bool(c.get("part"))
    store = Store(profile / "library.json")
    added = store.add_paths(folders, watched=True, now=4_000_000_000.0)
    got = {item.path: item for item in added}
    for path, c in expected.items():
        item = got.get(path)
        if item is None:
            problems.append(f"not imported: {path}")
        elif item.title != (c.get("title") or Path(path).stem):
            problems.append(f"{path}: title {item.title!r}, clips.json says {c.get('title')!r}")
    for path in got:
        if path in rejected:
            problems.append(f"a rejected clip was imported: {path}")
        elif path not in expected:
            problems.append(f"imported something clips.json doesn't list as ready: {path}")
    again = store.add_paths(folders, watched=True, now=4_000_000_000.0)
    if again:
        problems.append(f"{len(again)} clips imported twice")
    reports = watch_report(store, [str(f) for f in folders], now=4_000_000_000.0)
    counted = sum(r.clips for r in reports)
    if counted != len(expected) or sum(r.in_app for r in reports) != len(got):
        problems.append(f"Ready page counts {counted} clips ({sum(r.in_app for r in reports)} in the app); "
                        f"{len(expected)} expected, {len(got)} imported")
    result = {"folders": [str(f) for f in folders], "ready": len(expected), "imported": len(got),
              "rejected_left_out": len(rejected), "from_requests": requests, "series_parts": parts,
              "problems": problems}
    print(json.dumps(result, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
