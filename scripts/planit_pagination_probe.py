"""Diagnose PlanIt paging behaviour: does `offset` work? Is ordering stable?

Docs (planit.org.uk/api) document `index` (0-based) / `page` (1-based) and a
`sort` param, but NOT `offset` — which is what pam.api.fetch_area uses.
They also promise `from`/`to`/`total` meta fields our fetch loop ignores.

Runs a handful of single-page probes against one tiny Haringey window,
60s+ apart (pam.api pacing), and prints meta + record identity/order so we
can see: offset vs index vs page, default sort, and whether re-issuing the
same request minutes later returns the same records in the same order.

    .venv\\Scripts\\python.exe scripts\\planit_pagination_probe.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["PLANIT_WINDOW_START"] = "0"   # suspend overnight window for probes
os.environ["PLANIT_WINDOW_END"] = "24"

from pam.api import PlanItPaused, _get  # noqa: E402
from pam.config import load_config  # noqa: E402

BASE = {"auth": "Haringey", "start_date": "2026-06-01",
        "end_date": "2026-06-02", "pg_sz": 5}


def get(params: dict, cfg) -> dict:
    """_get, but wait out daytime rate-limit penalties instead of dying."""
    import time
    from datetime import datetime
    for _ in range(3):
        try:
            return _get(params, cfg)
        except PlanItPaused as exc:
            wait = (exc.resume_at - datetime.now(exc.resume_at.tzinfo)
                    ).total_seconds() + 65
            print(f"paused ({exc}); sleeping {wait:.0f}s")
            time.sleep(max(wait, 65))
    return _get(params, cfg)


def show(tag: str, data: dict) -> list[str]:
    recs = data.get("records") or []
    meta = {k: v for k, v in data.items() if k != "records"}
    print(f"\n--- {tag} ---")
    print("meta:", meta)
    names = []
    for r in recs:
        names.append(r.get("name"))
        print(f"  {r.get('name')}  start={r.get('start_date')}  "
              f"last_changed={r.get('last_changed')}  "
              f"last_scraped={r.get('last_scraped')}")
    return names


def main() -> None:
    cfg = load_config()
    base = show("page1 (no paging param)", get(dict(BASE), cfg))
    off = show("offset=5", get({**BASE, "offset": 5}, cfg))
    idx = show("index=5", get({**BASE, "index": 5}, cfg))
    pg = show("page=2", get({**BASE, "page": 2}, cfg))
    srt = show("sort=start_date (asc)", get({**BASE, "sort": "start_date"}, cfg))
    srt2 = show("sort=start_date.asc,name.asc",
                get({**BASE, "sort": "start_date.asc,name.asc"}, cfg))
    again = show("page1 again (~7 min later)", get(dict(BASE), cfg))

    print("\n=== VERDICT ===")
    print("offset works:", off != base, "| index works:", idx != base,
          "| page works:", pg != base)
    print("offset==index:", off == idx, "| index==page2:", idx == pg)
    print("sort asc respected (first start_date ascending):", srt)
    print("multi-key sort accepted:", srt2)
    print("page1 stable over ~7min:", again == base)
    if again != base:
        print("  first call:", base)
        print("  later call:", again)


if __name__ == "__main__":
    main()
