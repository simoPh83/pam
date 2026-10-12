# PAM — Agent Context

Planning Application Monitor. This repo is the **Python worker** (runs on
Railway): it pulls planning applications from the PlanIt API into Supabase
Postgres and owns all grouping/parent-hunt logic.

## Repositories

- **This repo:** [simoPh83/pam](https://github.com/simoPh83/pam) — worker, grouping engine, SQL/RPC migrations.
- **Sibling (web UI):** [simoPh83/pam-client](https://github.com/simoPh83/pam-client) — Next.js + Supabase on Vercel. Read-only on fetcher tables; writes go through RPCs (e.g. `apply_grouping_resolution`) or UI-owned tables. Specs for pam-client features live in this repo under `docs/` and `web-client-kickoff/`.

## Connecting to Supabase from this repo

- Credentials live in the gitignored `.env` at the repo root — **never commit it, never print its values**.
- Standard pattern (used by `pam/` and every `scripts/*.py`):

  ```python
  from pathlib import Path
  import os, psycopg
  from pam.config import load_dotenv

  load_dotenv(Path(".env"))                    # real env vars win over .env
  conn = psycopg.connect(os.environ["DATABASE_URL"])
  ```

- `.env` keys:
  - `DATABASE_URL` — direct (session) connection. Use this for all worker/script/ad-hoc work.
  - `TRANSACTION_DB_STRING` — Supabase **transaction pooler** (port 6543); for serverless/external clients (e.g. Vercel), not needed for repo scripts (psycopg scripts use `DATABASE_URL`).
- psycopg gotchas: escape literal `%` as `%%` in SQL (`LIKE 'unlinked%%'`); cast list params with `::text[]` / use `= ANY(%s)` with a Python list.
- RLS is already applied to fetcher tables (`scripts/rls.sql`): `anon` has no access, `authenticated` is SELECT-only. **Do not weaken it.** UI writes happen via SECURITY DEFINER RPCs.

## Hard rules

- The worker owns the fetcher tables (`applications`, `state_history`, `projects`, `project_applications`, `grouping_review`, `missing_parents`, …). pam-client never writes to them directly.
- After changing parser/grouping logic in `pam/projects.py`: running processes keep the old module — re-run `scripts/regroup_missed_parent_refs.py` (idempotent) and restart the worker. See `docs/2026-10-12-queue-case-review-playbook.md` for the full deploy sequence and regression suite.

## Key docs (read before related work)

- `docs/2026-10-12-queue-case-review-playbook.md` — diagnosing `unlinked_*` queue rows; standard inspection snippet, decision tree, parser regression suite.
- `docs/2026-10-12-no-scheme-queue-notes.md` — queue model and format catalogue.
- `docs/2026-10-10-scheme-grouping-spec.md` / `docs/2026-10-10-grouping-review-ui-spec.md` — grouping model and its UI contract.
- `docs/2026-10-12-manual-resolution-ui-spec.md` — manual resolution of unlinked applications (`resolve_review_with_ref` / `resolve_review_singleton` RPCs, `manual_parent_refs`, `role_override`).
- `web-client-kickoff/00-README-START-HERE.md` — pam-client stack, RLS tiers, hard rules.
- `README.md` — setup and run commands.
