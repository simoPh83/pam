"""Validate pam.classify against the live corpus (eyeball harness).

Classifies every project by its MAIN application = earliest role='original'
member (the most accurate description). Projects with NO original member are
fragment chains whose main was never fetched — POSTPONE, never auto-purge.

Reports: distributions, purge/keep/postpone sets with random samples to
eyeball, per-authority breakdown, leads impact, record-level ingest
simulation, and projected bytes reclaimed.

Read-only. Run:  .venv\\Scripts\\python.exe scripts\\analyze_project_descriptions.py
"""

from __future__ import annotations

import os
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pam.classify import is_minor, keep_reasons, minor_hits  # noqa: E402
from pam.config import load_dotenv  # noqa: E402

PARENT_CITATION = re.compile(
    r"\b(pursuant to|condition(s)? \w* ?\(?\d|planning permission (ref[.: ]?)?"
    r"\w{2,}/\d{4}/|[A-Z]{2,4}/\d{4}/\d{3,})\b")


def main() -> None:
    load_dotenv(Path(".env"))
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)

    print("Loading projects with their MAIN application (earliest original) ...")
    # One row per project: earliest role='original' member when present,
    # else earliest member of any role (is_orig=False -> postpone set).
    rows = conn.execute(
        "SELECT DISTINCT ON (pa.project_id) "
        "       p.id, p.authority, p.n_applications, p.grouping_state, "
        "       a.uid, a.description, a.app_type, a.app_size, "
        "       a.development_type, a.n_dwellings, a.app_state, a.start_date, "
        "       (pa.role = 'original') AS is_orig "
        "FROM projects p "
        "JOIN project_applications pa ON pa.project_id = p.id "
        "JOIN applications a ON a.uid = pa.uid "
        "ORDER BY pa.project_id, (pa.role = 'original') DESC, "
        "         a.start_date ASC NULLS LAST, a.uid"
    ).fetchall()
    print(f"  {len(rows):,} projects")
    total_apps = conn.execute("SELECT count(*) FROM applications").fetchone()[0]
    total_members = conn.execute("SELECT count(*) FROM project_applications").fetchone()[0]
    print(f"  {total_apps:,} applications | {total_members:,} memberships")

    # ---- distributions ------------------------------------------------------
    def dist(title, key_fn, top=40):
        c = Counter(key_fn(r) for r in rows)
        print(f"\n=== {title} ===")
        for val, n in c.most_common(top):
            print(f"  {n:7,}  {val!r}")

    dist("MAIN app_type", lambda r: r[6])
    dist("MAIN app_size", lambda r: r[7])
    dist("grouping_state", lambda r: r[3])

    # ---- classification -------------------------------------------------------
    purge, keep, grey, postpone = [], [], [], []
    for r in rows:
        (pid, auth, nmem, gstate, uid, desc, atype, size, devt, ndwell,
         state, sdate, is_orig) = r
        if not is_orig:
            postpone.append(r)
            continue
        hits = minor_hits(desc, atype)
        reasons = keep_reasons(desc, size, ndwell, hits)
        if is_minor(desc, atype, size, ndwell):
            purge.append((r, hits))
        elif hits and reasons:
            grey.append((r, hits, reasons))
        else:
            keep.append(r)

    purged_apps = sum(r[2] for r, _ in purge)
    postponed_apps = sum(r[2] for r in postpone)
    print("\n=== CLASSIFICATION (by MAIN application description) ===")
    print(f"  PURGE    projects: {len(purge):7,} ({len(purge)/len(rows)*100:4.1f}%) "
          f"holding {purged_apps:,} applications")
    print(f"  GREY     (minor+keep -> KEEP): {len(grey):7,}")
    print(f"  KEEP     projects: {len(keep):7,}")
    print(f"  POSTPONE (main not in DB):   {len(postpone):7,} "
          f"holding {postponed_apps:,} applications")
    print("\n  purge set by buckets fired (multi-label):")
    for k, n in Counter(h for _r, hits in purge for h in hits).most_common():
        print(f"    {n:7,}  {k}")

    citing = [(r, h) for r, h in purge if PARENT_CITATION.search(r[5] or "")]
    print(f"\n  purge candidates whose MAIN cites a parent ref: {len(citing):,}")
    print("  purge by app_type:",
          dict(Counter(r[6] for r, _ in purge).most_common()))
    print("  purge by app_state:",
          dict(Counter(r[10] for r, _ in purge).most_common()))

    by_auth = defaultdict(lambda: [0, 0])
    for r, _h in purge:
        by_auth[r[1]][0] += 1
    for r in rows:
        by_auth[r[1]][1] += 1
    print("\n=== PURGE BY AUTHORITY ===")
    for auth, (p, t) in sorted(by_auth.items(), key=lambda kv: -kv[1][1]):
        print(f"  {auth or '?':<28} {p:6,} / {t:6,}  ({p / t * 100:4.1f}%)")

    lead_uids = {x[0] for x in conn.execute(
        "SELECT uid FROM applications WHERE in_leads_sheet")}
    leads_per_project = dict(conn.execute(
        "SELECT pa.project_id, count(*) FROM project_applications pa "
        "JOIN applications a ON a.uid = pa.uid "
        "WHERE a.in_leads_sheet GROUP BY pa.project_id").fetchall())
    purge_lead_members = sum(leads_per_project.get(r[0], 0) for r, _ in purge)
    print(f"\n  lead rows inside purge projects: {purge_lead_members:,} "
          f"of {len(lead_uids):,} leads total")

    random.seed(7)
    print("\n=== PURGE SAMPLES (30 random — eyeball for false purges) ===")
    for r, hits in random.sample(purge, min(30, len(purge))):
        print(f"  [{','.join(hits)}] ({r[6]}/{r[7]}) {(r[5] or '')[:120]!r}")
    print("\n=== GREY SAMPLES (20 random — kept: minor phrase + keep signal) ===")
    for r, hits, reasons in random.sample(grey, min(20, len(grey))):
        print(f"  [{','.join(hits)}] KEEP={','.join(reasons)} "
              f"({r[6]}/{r[7]}) {(r[5] or '')[:110]!r}")
    print("\n=== KEEP SAMPLES (15 random — no minor hit at all) ===")
    for r in random.sample(keep, min(15, len(keep))):
        print(f"  ({r[6]}/{r[7]}/{r[8]}) {(r[5] or '')[:120]!r}")
    print("\n=== POSTPONE SAMPLES (10 random — no original member) ===")
    for r in random.sample(postpone, min(10, len(postpone))):
        print(f"  ({r[6]}/{r[7]}/{r[3]}) {(r[5] or '')[:120]!r}")

    # ---- description length safeguard calibration --------------------------
    lens = sorted(len(r[5] or "") for r, _ in purge)
    if lens:
        def pct(p):
            return lens[min(len(lens) - 1, int(len(lens) * p))]
        print("\n=== PURGE-CANDIDATE description length (chars) ===")
        print(f"  p50={pct(.5)} p75={pct(.75)} p90={pct(.9)} "
              f"p95={pct(.95)} p99={pct(.99)} max={lens[-1]}")
        for thresh in (200, 300, 400, 500, 750, 1000):
            over = sum(1 for n in lens if n >= thresh)
            print(f"  >= {thresh:5} chars: {over:6,} purge candidates "
                  f"({over / len(lens) * 100:.1f}%)")
        long_purge = sorted(purge, key=lambda rh: -len(rh[0][5] or ""))[:15]
        print("\n=== LONGEST purge-candidate descriptions (eyeball!) ===")
        for r, hits in long_purge:
            print(f"  [{','.join(hits)}] len={len(r[5] or '')} "
                  f"({r[6]}/{r[7]}) {(r[5] or '')[:180]!r}")

    # ---- record-level ingest simulation ---------------------------------------
    print("\nLoading all applications for record-level ingest simulation ...")
    allrows = conn.execute(
        "SELECT uid, description, app_type, app_size, n_dwellings "
        "FROM applications").fetchall()
    blocked = citer = 0
    for uid, desc, atype, size, ndwell in allrows:
        d = desc or ""
        if PARENT_CITATION.search(d):
            citer += 1
            continue
        if is_minor(desc, atype, size, ndwell):
            blocked += 1
    print(f"  {len(allrows):,} records: {blocked:,} would be blocked at ingest "
          f"({blocked / len(allrows) * 100:.1f}%), {citer:,} parent-citers exempt")

    # ---- size projection ------------------------------------------------------
    apps_total = conn.execute(
        "SELECT pg_total_relation_size('applications')").fetchone()[0]
    per_app = apps_total / max(total_apps, 1)
    saved = purged_apps * (per_app + 264) + len(purge) * (370 + 500)
    tombstone = len(purge) * 150 + purged_apps * 60
    print(f"\n=== SIZE PROJECTION ===")
    print(f"  purge frees ~{saved / 1e6:,.0f} MB; tombstones cost "
          f"~{tombstone / 1e6:,.0f} MB; net ~{(saved - tombstone) / 1e6:,.0f} MB")

    conn.close()


if __name__ == "__main__":
    main()
