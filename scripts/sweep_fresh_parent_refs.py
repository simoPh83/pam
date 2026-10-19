"""One-off: full fresh-parse sweep after parser updates.

Context: docs/2026-10-19-backend-issue-stale-flags-and-hunt-queue-gap.md
item 3. The stored parent_ref is only the first ref found at group() time,
so a stored-ref-scoped backfill misses refs the current parser finds on
members whose stored state gives no hint (e.g. new secondary refs), and it
leaves memberships untouched where the parser's verdict changed.

This sweep re-parses every project member's description with the current
parent_refs_of() and:

1. re-runs group() on members whose stored evidence disagrees with the
   fresh parse (has_parent_refs flipped, or the stored first ref no longer
   parses) — group() fixes membership, enqueues unresolved refs (pass 4b)
   and refreshes the touched projects;
2. refresh()es projects whose members cite a fresh-parsed ref that neither
   resolves in applications nor has a missing_parents row — refresh() now
   enqueues those refs itself, healing the hunt queue and the chain flags
   without moving memberships (covers fresh secondary refs on members whose
   stored parent_ref still parses).

Idempotent; safe to re-run. --dry-run reports the counts without writing.

Usage: python scripts/sweep_fresh_parent_refs.py [--dry-run]
"""
import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pam.config import load_dotenv  # noqa: E402
from pam.ledger import Ledger  # noqa: E402
from pam.projects import group, parent_refs_of, refresh  # noqa: E402

BATCH = 500


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(".env"))
    ledger = Ledger(os.environ["DATABASE_URL"])
    conn = ledger.conn

    # Phase 1: re-parse every member's description with the current parser
    drifted = []                              # uids to re-group
    fresh = defaultdict(dict)                 # auth -> ref -> (uid, pid)
    n_members = 0
    with conn.cursor(name="sweep") as cur:    # server-side: full-table scan
        cur.execute(
            "SELECT pa.uid, pa.project_id, a.authority, a.description, "
            "       pa.parent_ref, pa.has_parent_refs "
            "FROM project_applications pa "
            "JOIN applications a ON a.uid = pa.uid")
        while rows := cur.fetchmany(5000):
            n_members += len(rows)
            for uid, pid, auth, desc, stored, hpr in rows:
                refs = parent_refs_of(desc, auth) if desc else []
                if bool(refs) != hpr or (stored and stored not in refs):
                    drifted.append(uid)
                for ref in refs:
                    fresh[auth].setdefault(ref, (uid, pid))
            if n_members % 50000 == 0:
                print(f"  scanned {n_members} members", flush=True)
    print(f"{n_members} members: {len(drifted)} drifted (stored vs fresh "
          f"parse), {sum(len(r) for r in fresh.values())} distinct fresh refs")

    # Phase 2: which fresh refs resolve in applications? (chunked per authority)
    unresolved = {}  # (auth, ref) -> (uid, pid)
    for auth, refs in fresh.items():
        names = list(refs)
        for i in range(0, len(names), 5000):
            chunk = names[i:i + 5000]
            found = {r[0] for r in conn.execute(
                "SELECT cand.ref FROM unnest(%s::text[]) AS cand(ref) "
                "WHERE EXISTS (SELECT 1 FROM applications a "
                "  WHERE a.authority = %s "
                "    AND (upper(a.reference) = cand.ref "
                "         OR starts_with(upper(a.reference), cand.ref || '/')))",
                (chunk, auth))}
            for ref in chunk:
                if ref not in found:
                    unresolved[(auth, ref)] = refs[ref]

    queued = {(a, r) for a, r in conn.execute(
        "SELECT authority, reference FROM missing_parents")}
    missing = {k: v for k, v in unresolved.items() if k not in queued}
    stale_pids = sorted({pid for _uid, pid in missing.values()})
    print(f"{len(unresolved)} unresolved fresh refs "
          f"({len(queued & unresolved.keys())} already queued, "
          f"{len(missing)} missing a queue row) across {len(stale_pids)} "
          "projects")

    if args.dry_run:
        conn.rollback()
        print("dry run — rolled back (no group/refresh performed)")
        ledger.close()
        return

    # Drifted members: full re-group (membership + queue + flags)
    print(f"re-grouping {len(drifted)} drifted members")
    for i in range(0, len(drifted), BATCH):
        group(drifted[i:i + BATCH], conn)
        print(f"  {min(i + BATCH, len(drifted))}/{len(drifted)}", flush=True)

    # Projects with unqueued fresh refs but no membership drift: refresh only
    print(f"refreshing {len(stale_pids)} projects with unqueued fresh refs")
    for i in range(0, len(stale_pids), BATCH):
        refresh(conn, stale_pids[i:i + BATCH])
        print(f"  {min(i + BATCH, len(stale_pids))}/{len(stale_pids)}",
              flush=True)

    remaining = conn.execute(
        "SELECT count(*) FROM missing_parents WHERE status = 'pending'"
    ).fetchone()[0]
    conn.commit()
    print(f"committed; pending missing_parents rows now: {remaining}")
    ledger.close()


if __name__ == "__main__":
    main()
