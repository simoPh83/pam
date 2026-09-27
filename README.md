# PAM — Planning Application Monitor

Monitors planning applications via the [PlanIt](https://www.planit.org.uk) public API, tracks them in a local ledger, and maintains a spreadsheet of approved applications (leads) for photography outreach.

## Setup (any device)

Requires Python 3.11+.

```powershell
# from the project folder
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate           # macOS/Linux

pip install -r requirements.txt
```

## Run

```powershell
python -m pam.main                    # normal run (writes data\leads.xlsx + ledger)
python -m pam.main --dry-run          # fetch and report, write nothing
python -m pam.main --no-backfill      # only the last 14 days
python -m pam.main --from 2026-06-01 --to 2026-09-27
python -m pam.main --area local       # run one named area only
python -m pam.main --states Withdrawn # also fetch extra states
python -m pam.main --help             # all options
```

CLI flags override [config.yaml](config.yaml) — areas, lead states, backfill window, API pacing and output paths all live there.

## Scheduling (Windows Task Scheduler)

Daily at 07:00, for example:

```
Program:  d:\Python playfolder\PAM\.venv\Scripts\python.exe
Args:     -m pam.main --no-backfill
Start in: d:\Python playfolder\PAM
```

## Outputs (gitignored, in `data/`)

- `leads.xlsx` — one row per approved application (Permitted/Conditions), with distance from home, agent/applicant where available, and links to the council record
- `ledger.sqlite` — every application ever seen, incl. Undecided ones watched for state changes

## Docs

Design decisions and verified API findings live in [docs/](docs).
