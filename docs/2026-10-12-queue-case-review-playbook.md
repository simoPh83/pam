# Playbook: case-by-case review of the "No linked scheme" queue

**For:** future sessions investigating `unlinked_no_scheme` / `unlinked_multi_scheme`
rows one at a time (user pastes a lead from pam-client, we diagnose here).
**Created:** 2026-10-12, after the first review session that produced six
parser fixes and two role-classifier fixes.
**Related:** `2026-10-12-no-scheme-queue-notes.md` (queue model + what was
fixed), `2026-10-10-scheme-grouping-spec.md` (the grouping model).

## The model in one paragraph

A queue row should exist **iff** the application has no parseable parent ref
AND its address matches no known scheme. Apps with refs that don't resolve
are `provisional` projects owned by `missing_parents`/parent-hunt — they are
never queue rows. No-ref non-originals at a single-scheme address are
auto-attached (`linked_by='inferred'`). So every queue row is either (a) a
genuine orphan (dismiss), or (b) a parser/classifier bug to fix.

## Standard inspection (run this first, every time)

Paste-adapt; always from the `pam` repo root with the venv python. Read-only:

```python
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from pam.config import load_dotenv
from pam.projects import parent_refs_of, role_of, address_key
import psycopg
load_dotenv(Path(".env"))
conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)

uid = "AUTH/YY/NNNN/XXX"   # <-- the case
auth, desc, app_type, addr, pc = conn.execute(
    "SELECT authority, description, app_type, address, postcode "
    "FROM applications WHERE uid=%s", (uid,)).fetchone()
refs = parent_refs_of(desc, auth)
print("refs:", refs, "| role:", role_of(desc, app_type, auth))
print("membership:", conn.execute(
    "SELECT project_id, parent_ref, role, has_parent_refs, linked_by "
    "FROM project_applications WHERE uid=%s", (uid,)).fetchall())
print("review:", conn.execute(
    "SELECT reason, status, resolution FROM grouping_review "
    "WHERE uid=%s AND status='pending'", (uid,)).fetchall())
print("missing_parents:", conn.execute(
    "SELECT reference, status FROM missing_parents WHERE requested_by=%s",
    (uid,)).fetchall())
for r in refs:
    print("parent", r, "stored?", conn.execute(
        "SELECT uid FROM applications WHERE authority=%s "
        "AND (upper(reference)=%s OR starts_with(upper(reference), %s || '/'))",
        (auth, r, r)).fetchall())
# same-address check (only when refs is empty and you suspect a bucket miss)
key = address_key(addr, pc)
rows = conn.execute(
    "SELECT uid, app_type, address, postcode FROM applications "
    "WHERE authority=%s", (auth,)).fetchall()
print("same address_key:", [(u, t) for u, t, a, p in rows
      if u != uid and address_key(a, p) == key])
```

## Decision tree

1. **Refs parse and parent is stored** → stale queue state. The row predates
   the fix that made the ref parseable. `group()` re-run (via
   `scripts/regroup_missed_parent_refs.py`) links it and Pass 4c auto-resolves
   the review row. No code change.
2. **Refs parse, parent not stored, no `missing_parents` row** → same stale
   state; the sweep will catalogue the ref for the parent hunt. If a
   `missing_parents` row DOES exist and is `pending`/`exhausted`: correct
   behaviour — hunt will resolve it (`pending`) or already missed
   definitively (`exhausted`, stays `provisional`).
3. **No refs, but the description visibly contains a council ref** → parser
   gap. Isolate which of the five regexes fails
   (`PARENT_RE`, `CONDITION_OF_RE`, `DATED_REF_RE`, `VARIED_BY_RE`,
   `REF_PREFIX_RE` — print `[m.group(1) for m in rx.finditer(desc)]` for
   each), extend it *narrowly*, then ALWAYS:
   - run the regression suite below (append the new case to it),
   - measure queue impact (query below) and record the new format in
     `2026-10-12-no-scheme-queue-notes.md`.
4. **No refs, no visible ref, role should be `original` but isn't** →
   classifier bug (see the building-services false positives: "air condition
   unit", "discharge flue/terminals"). Extend `_BUILDING_SERVICES_RE`, then
   `scripts/promote_standalone_originals.py` sweeps the queue.
5. **No refs, no visible ref, non-original role, address matches nothing** →
   genuine orphan: the queue is working as intended; user's action is
   *dismiss*. Note: LDOs and pre-digital (pre-2000) parents can never
   resolve — expect these to be permanent.
6. **No refs, non-original, but a scheme clearly exists under a different
   address form** (e.g. Southwark "Street Record …") → data limitation, not
   a bug; queue stays correct; candidate for a future "search & attach" UI.

## Regression suite (append every new format — keep ordered)

```python
from pam.projects import parent_refs_of as p
from pam.projects import role_of
CASES = [
  ("approved under Ref: 23/02239/OUT", ["23/02239/OUT"]),
  ("(RN 24/05892/FULL)", ["24/05892/FULL"]),
  ("(RN: 25/03133/FULL)", ["25/03133/FULL"]),
  ("(RN25/02937/FULL)", ["25/02937/FULL"]),                # no-space RN
  ("dated 22/05/2025 ref 2025/1231", ["2025/1231"]),
  ("planning permission ref no. 25/AP/0349", ["25/AP/0349"]),
  ("planning permission dated 2 September 2022 PP/21/06400", ["PP/21/06400"]),
  ("planning permission dated 1st October 2024 (24/00152/FULL)", ["24/00152/FULL"]),
  ("planning permission dated 02 September 1964 (RN: 101822/C)", ["101822/C"]),  # legacy numeric
  ("condition 02, 03 and 04 of application: 22/03720/FUL", ["22/03720/FUL"]),
  ("conditions 1(a), 2 and 10 of application 20/01234/FUL", ["20/01234/FUL"]),
  ("Discharge of condition 3 of planning permission PP/00/02566", ["PP/00/02566"]),
  ("Ref: 22/00516/FUL (varied by 24/03476/VAR)", ["24/03476/VAR", "22/00516/FUL"]),
  ("Non-material amendment to application 2018/1234/P", ["2018/1234/P"]),
  # junk guards — must stay empty
  ("planning permission dated 2020", []),
  ("see drawing ref 12/05/2023 for details", []),
  ("see MODERN25/1234 for context", []),
  ("Single storey rear extension", []),
]
ROLE_CASES = [  # (description, app_type, expected_role)
  ("installation of air condition unit to side", "Full", "original"),
  ("ductwork, terminating through an external louvre for the discharge of extracted air", "Full", "original"),
  ("kitchen extract system, roof level discharge terminals", "Full", "original"),
  ("Discharge of conditions 3, 4 and 5 of PP/00/02566", "Conditions", "discharge"),
  ("Erection of single storey extension", "Full", "original"),
]
for d, want in CASES:
    got = p(d)
    assert sorted(got) == sorted(want), (d, want, got)
for d, t, want in ROLE_CASES:
    assert role_of(d, t) == want, (d, want)
print("ALL OK")
```

Test strings need realistic keyword context ("planning permission dated …") —
a bare "dated 1st October 2024 (ref)" with no keyword SHOULD return nothing.

## Queue impact measurement (after any parser/role change)

```python
rows = conn.execute(
    "SELECT g.uid, g.authority, a.description, a.app_type "
    "FROM grouping_review g JOIN applications a ON a.uid=g.uid "
    "WHERE g.status='pending' AND g.reason LIKE 'unlinked%'").fetchall()
print("queue:", len(rows),
      "| now yield refs:", sum(1 for _u,a,d,_t in rows if parent_refs_of(d,a)))
from collections import Counter
print(Counter(role_of(d, t, a) for _u, a, d, t in rows))
```

## Gotchas learned on 2026-10-12

- **Module snapshot**: every running process (worker, a mid-run script) uses
  the parser as of ITS start. After any parser fix: re-run
  `scripts/regroup_missed_parent_refs.py` (it only touches still-ref-less
  members, so it's cheap and idempotent), then restart the worker.
- **psycopg**: `%` in SQL strings (`LIKE 'unlinked%'`) must be `%%`; unnest
  params need `::text[]` casts.
- **Temp tables**: `ON COMMIT DROP` doesn't help when group() is called in
  batches inside one transaction — DROP explicitly.
- Queue rows are all still project members (the worker re-groups them), so
  member-based sweeps cover them; the review row is resolved separately.
- Deploy sequencing: stop worker → sweeps/scripts → restart worker, so a
  stale worker doesn't recreate what the script just fixed.
