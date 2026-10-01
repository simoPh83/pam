# Resume process — interrupted backfills

Backfills are resumable: every fetched page is committed to
`data/ledger.sqlite` immediately, and `fetch_progress` records the last
completed offset per (window, area, state). Stopping mid-run (Ctrl+C, or even
a machine shutdown) loses at most the single in-flight page.

## To resume on any machine

1. Pull the repo **and make sure `data/ledger.sqlite` is included** — the
   progress checkpoints live in the DB, not in git (add it to the commit if
   it's currently ignored).
2. Activate the venv, then re-run the **exact same command** — the progress
   table skips completed states and resumes the interrupted one at its saved
   offset:

```powershell
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe -m pam.main --area westminster --from 2023-10-01 --log logs/westminster-resume.log
```

The `--from` date must match the original run — it forms the `run_window`
key (`2023-10-01_2026-10-01`) that the resume logic looks up. A different
`--from` starts a fresh window instead of resuming.

## Current state (2026-10-01)

| Borough | Window | Status |
|---|---|---|
| islington | 2023-09-30 → 2026-09-30 | ✅ complete |
| camden | 2023-10-01 → 2026-10-01 | ✅ complete |
| westminster | 2023-10-01 → 2026-10-01 | ⏸️ Permitted (8,700) + Conditions (200) done; **Undecided resumes at offset 12,500** |

## Tips

- Run the resume in a fresh session — PlanIt's rate limiter is cumulative;
  resuming immediately after a throttled session inherits the cooldown.
- Check progress without hitting the API:
  `SELECT * FROM fetch_progress WHERE area='westminster';`
- `done=1` means that state finished; `done=0` with a `last_offset` is where
  the resume picks up.
