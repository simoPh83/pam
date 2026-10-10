"""Group applications into projects (development sites).

One physical development produces many applications (original permission,
s73 variations, condition discharges, NMAs). Outreach is per project, not per
application. Clustering is by (authority, normalised address); parent
references extracted from descriptions are kept for debugging/merges.

See docs/2026.10.04 project-grouping-spec.md.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

log = logging.getLogger("pam.projects")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    authority      TEXT NOT NULL,
    name           TEXT,
    address_key    TEXT NOT NULL,
    root_uid       TEXT,
    latest_uid     TEXT,
    n_applications INTEGER NOT NULL DEFAULT 1,
    first_seen     DATE,
    last_updated   DATE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (authority, address_key)
);
CREATE TABLE IF NOT EXISTS project_applications (
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    uid        TEXT NOT NULL,
    parent_ref TEXT,
    is_root    BOOLEAN NOT NULL DEFAULT false,
    role       TEXT,
    has_parent_refs BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (project_id, uid)
);
CREATE TABLE IF NOT EXISTS project_aliases (
    authority   TEXT NOT NULL,
    address_key TEXT NOT NULL,
    project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    PRIMARY KEY (authority, address_key)
);
CREATE INDEX IF NOT EXISTS idx_project_applications_uid ON project_applications (uid);
CREATE TABLE IF NOT EXISTS missing_parents (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    authority    TEXT NOT NULL,
    reference    TEXT NOT NULL,          -- unresolved parent ref (upper-case)
    ref_year     SMALLINT,               -- parsed from the ref, if possible
    requested_by TEXT,                   -- uid of the child application citing it
    project_id   BIGINT REFERENCES projects(id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|exhausted (found rows are deleted)
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    found_uid    TEXT,
    UNIQUE (authority, reference)
);
"""

# Columns added after first release — applied idempotently by ensure_schema().
MIGRATIONS = (
    "ALTER TABLE project_applications ADD COLUMN IF NOT EXISTS "
    "has_parent_refs BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS root_in_db BOOLEAN",
)


def ensure_schema(conn) -> None:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    for stmt in MIGRATIONS:
        conn.execute(stmt)

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s/-]")
_UNIT = re.compile(r"\b(?:FLAT|UNIT|APARTMENT|ROOM|APPT)\s+\w+\s*,?\s*", re.I)
_POSTCODE = re.compile(r"\s+[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\s*$", re.I)

PARENT_RE = re.compile(
    r"(?:permission|approval|application|consent|amendments?|reference)\s*"
    r"(?:planning\s+)?"
    r"(?:ref(?:erence)?[.:]?\s*|no[.:]?\s*|\(\s*)?"
    r"([A-Z0-9][A-Z0-9/_.-]{4,25}\d[A-Z0-9/_.-]*)",
    re.I,
)

# "condition(s) <list> of <REF>" — a condition discharge that cites the parent
# by reference after the condition number(s), e.g. "contrary to condition 3 of
# PP/00/02566". Narrow (authority-style ref shapes only) so the condition list
# itself is never captured. Kept separate from PARENT_RE: widening the main
# pattern to tolerate the intervening number list caused large regressions.
CONDITION_OF_RE = re.compile(
    r"conditions?\s+[\d,\s&]+?of\s+"
    r"(?:planning\s+)?(?:permission|application|consent|ref(?:erence)?)?\s*"
    r"([A-Z]{1,3}/\d{2,4}/\d{3,6}(?:/[A-Z0-9]+)?)",
    re.I,
)


def address_key(address: str | None, postcode: str | None = None) -> str | None:
    if not address:
        return None
    key = _WS.sub(" ", address.upper().strip())
    if postcode:
        key = re.sub(r"\s+" + re.escape(postcode.upper()) + r"\s*$", "", key)
    key = _POSTCODE.sub("", key)
    key = _PUNCT.sub(" ", key)
    key = _UNIT.sub("", key)
    return _WS.sub(" ", key).strip() or None


def role_of(description: str | None, app_type: str | None = None) -> str:
    """Classify an application's role in its scheme.

    Reference-first, then precise keywords, then PlanIt app_type for
    conditions only:
      1. A parsed parent citation makes it a follow-up (variation / nma /
         discharge by keyword, else discharge). A real citation beats any type
         label — PlanIt's app_type=Full is used for resubmissions too.
      2. Keyword signals for the follow-up types (variation / nma / discharge)
         are precise phrases, not loose stems. Bare "pursuant to" is NOT a
         signal — it is legal boilerplate (ES regs, S106 schedules, the Act).
      3. app_type='Conditions' -> discharge is reliable. app_type='Amendment'
         is NOT trusted: PlanIt files Certificates of Lawfulness, HMO
         conversions and other standalone originals under it.
      4. Everything else is original.
    """
    text = " ".join(t for t in (description, app_type) if t).lower()
    is_var = ("variation of condition" in text or "s73" in text
              or "section 73" in text)
    is_nma = ("non-material amendment" in text or "non material amendment" in text
              or "s96a" in text or "section 96a" in text)
    is_dis = ("discharge" in text or "approval of details" in text
              or re.search(r"\bcondition\b", text))
    if parent_refs_of(description):
        if is_var:
            return "variation"
        if is_nma:
            return "nma"
        return "discharge"
    if is_var:
        return "variation"
    if is_nma:
        return "nma"
    if is_dis:
        return "discharge"
    if (app_type or "").strip() == "Conditions":
        return "discharge"
    return "original"


_YEAR4 = re.compile(r"(?:19|20)\d{2}")
_YEAR2 = re.compile(r"(?:^|/)(\d{2})/")


def ref_year_of(ref: str) -> int | None:
    """Year embedded in a council reference, e.g. P2018/2269/FUL → 2018,
    2018/1234/P → 2018, PA/18/01585 → 2018, 18/06246/FUL → 2018.
    Returns None for implausible years (condition numbers, drawing refs and
    other regex false positives) and for pre-2000 years: PlanIt's historical
    coverage starts 2000–2002 depending on the authority, so those refs can
    never resolve."""
    from datetime import date
    lo, hi = 2000, date.today().year + 1
    m = _YEAR4.search(ref)
    if m:
        year = int(m.group(0))
        return year if lo <= year <= hi else None
    m = _YEAR2.search(ref)
    if m:
        yy = int(m.group(1))
        year = 2000 + yy if yy < 30 else 1900 + yy
        return year if lo <= year <= hi else None
    return None


def parent_refs_of(description: str | None) -> list[str]:
    """All planning references mentioned in a description (deduped, ordered).

    The PlanIt feed occasionally corrupts punctuation/quotes to a literal '?'
    (e.g. "condition?8?...of planning?permission?PP/22/06589"); normalise
    those to spaces before matching so the keyword context still parses.
    """
    text = (description or "").replace("?", " ")
    refs, seen = [], set()
    for rx in (PARENT_RE, CONDITION_OF_RE):
        for m in rx.finditer(text):
            ref = m.group(1).upper().rstrip(".,);")
            if len(ref) >= 6 and any(ch.isdigit() for ch in ref) and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def _resolve_refs(refs: set[str], authority: str, conn) -> dict[str, str]:
    """Map each extracted ref to the stored reference whose application has
    the earliest start_date in this authority; refs with no match are absent.
    A cited ref also matches a stored reference carrying a type suffix —
    Tower Hamlets cites "PA/26/00475" while PlanIt stores "PA/26/00475/NC"."""
    if not refs:
        return {}
    return {r[0]: r[1] for r in conn.execute(
        "SELECT DISTINCT ON (cand.ref) cand.ref, upper(a.reference) "
        "FROM unnest(%s::text[]) AS cand(ref) "
        "JOIN applications a ON a.authority = %s "
        "  AND (upper(a.reference) = cand.ref "
        "       OR starts_with(upper(a.reference), cand.ref || '/')) "
        "ORDER BY cand.ref, a.start_date ASC NULLS LAST",
        (list(refs), authority))}


def group(uids, conn) -> int:
    """(Re)group the given application uids. Returns how many were grouped."""
    uids = list(uids)
    if not uids:
        return 0
    rows = conn.execute(
        "SELECT uid, authority, address, postcode, description, app_type "
        "FROM applications WHERE uid = ANY(%s)", (uids,)).fetchall()

    # Pass 1: normalise in memory
    prepared = {}  # (authority, address_key) -> list of (uid, name, role, refs)
    all_refs: dict[str, set[str]] = {}  # authority -> refs
    for uid, authority, address, postcode, description, app_type in rows:
        key = address_key(address, postcode)
        if not key or not authority:
            continue
        refs = parent_refs_of(description)
        prepared.setdefault((authority, key), []).append(
            (uid, (address or "").strip()[:100], role_of(description, app_type), refs))
        if refs:
            all_refs.setdefault(authority, set()).update(refs)

    # Pass 2: resolve refs per authority (one query each), pick earliest
    resolved = {a: _resolve_refs(refs, a, conn) for a, refs in all_refs.items()}

    # Pass 3: find/create projects. Reference-linked apps join the project of
    # the application they cite (references trump address). Unlinked apps fall
    # back to an existing project at the same address (the address bucket is a
    # *seed* only — refresh() splits it into reference components). A brand-new
    # original with no ref match and no address bucket becomes its own project.
    keys = list(prepared)
    alias = {(a, k): pid for a, k, pid in conn.execute(
        "SELECT al.authority, al.address_key, al.project_id FROM project_aliases al "
        "JOIN (SELECT * FROM unnest(%s::text[], %s::text[])) v(authority, address_key) "
        "ON v.authority = al.authority AND v.address_key = al.address_key",
        ([k[0] for k in keys], [k[1] for k in keys])).fetchall()}

    # address -> an existing project at that address (for the unlinked seed)
    addr_pid = {}
    if keys:
        for auth, key, pid in conn.execute(
                "SELECT v.authority, v.address_key, "
                "       (SELECT p.id FROM projects p "
                "        WHERE p.authority = v.authority "
                "          AND p.address_key = v.address_key "
                "        ORDER BY p.n_applications DESC NULLS LAST LIMIT 1) "
                "FROM (SELECT * FROM unnest(%s::text[], %s::text[])) "
                "     v(authority, address_key)",
                ([k[0] for k in keys], [k[1] for k in keys])):
            addr_pid[(auth, key)] = pid

    def target_project(auth, key, uid, refs):
        """Project for a new/changed app: cited scheme > address bucket > new."""
        known = [r for r in resolved.get(auth, {}) if r in refs]
        if known:
            target_ref = resolved[auth][known[0]]
            row = conn.execute(
                "SELECT pa.project_id FROM project_applications pa "
                "JOIN applications a ON a.uid = pa.uid "
                "WHERE a.authority = %s AND a.reference = %s LIMIT 1",
                (auth, target_ref)).fetchone()
            if row:
                return row[0]
        if (auth, key) in alias:
            return alias[(auth, key)]
        if addr_pid.get((auth, key)):
            return addr_pid[(auth, key)]
        # brand-new scheme (a fresh original with no resolvable parent)
        cur = conn.execute(
            "INSERT INTO projects (authority, address_key, grouping_state) "
            "VALUES (%s, %s, 'provisional') RETURNING id", (auth, key))
        pid = cur.fetchone()[0]
        alias[(auth, key)] = pid
        addr_pid[(auth, key)] = pid
        return pid

    # Pass 4: bulk-upsert memberships
    memberships = []
    for (auth, key), members in prepared.items():
        for uid, _name, role, refs in members:
            pid = target_project(auth, key, uid, refs)
            known = [r for r in resolved.get(auth, {}) if r in refs]
            memberships.append((pid, uid, known[0] if known else
                                (refs[0] if refs else None), role, bool(refs)))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO project_applications (project_id, uid, parent_ref, role, "
            "has_parent_refs) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (project_id, uid) DO UPDATE "
            "SET parent_ref = excluded.parent_ref, role = excluded.role, "
            "has_parent_refs = excluded.has_parent_refs",
            memberships)

    # Pass 4b: record unresolved refs for the parent hunt (scoped later to
    # recently-active projects; here we just keep the catalogue complete)
    pid_of_uid = {uid: pid for pid, uid, *_ in memberships}
    unresolved = []  # (authority, reference, ref_year, child uid, project id)
    for (auth, key), members in prepared.items():
        for uid, _name, _role, refs in members:
            for ref in refs:
                if ref not in resolved.get(auth, {}):
                    unresolved.append((auth, ref, ref_year_of(ref), uid,
                                       pid_of_uid.get(uid)))
    if unresolved:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO missing_parents "
                "(authority, reference, ref_year, requested_by, project_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (authority, reference) DO NOTHING",
                unresolved)

    # Pass 5: refresh aggregates + root/state for touched projects
    pids = sorted({pid for pid, *_ in memberships})
    refresh(conn, pids)

    grouped = len(memberships)
    log.info("Grouped %s applications into %s projects", grouped, len(pids))
    return grouped


def refresh(conn, pids) -> None:
    """Recompute aggregates and root for the given projects.

    Aggregates (n_applications, latest_uid, name, first_seen, last_updated,
    root_in_db) come from set-based SQL over members. root_uid and
    grouping_state follow the reference-component structure: the root is the
    member with no *resolved* parent ref; multiple candidate roots mark the
    project ambiguous. A project with no unresolved refs anywhere is clean.
    """
    conn.execute(
        """
        UPDATE projects p SET
            n_applications = s.n,
            latest_uid     = s.latest_uid,
            name           = s.name,
            first_seen     = s.first_seen,
            last_updated   = s.last_updated
        FROM (
            SELECT pa.project_id,
                   count(*) AS n,
                   (array_agg(a.uid ORDER BY
                       coalesce(a.decided_date, a.last_updated) DESC NULLS LAST,
                       a.uid))[1] AS latest_uid,
                   (array_agg(a.address ORDER BY length(a.address))
                       FILTER (WHERE nullif(btrim(a.address), '') IS NOT NULL))[1]
                       AS name,
                   min(a.first_seen) AS first_seen,
                   max(a.last_updated) AS last_updated
            FROM project_applications pa
            JOIN applications a ON a.uid = pa.uid
            WHERE pa.project_id = ANY(%s)
            GROUP BY pa.project_id
        ) s
        WHERE p.id = s.project_id AND p.id = ANY(%s)
        """, (pids, pids))
    # root_in_db: no member still waiting on an unresolved parent ref
    conn.execute(
        "UPDATE projects p SET root_in_db = NOT EXISTS ("
        "  SELECT 1 FROM project_applications pa "
        "  JOIN missing_parents mp ON mp.authority = p.authority "
        "    AND mp.reference = pa.parent_ref AND mp.status <> 'found'"
        "  WHERE pa.project_id = p.id) "
        "WHERE p.id = ANY(%s)", (pids,))

    # root + grouping_state from the reference structure (one project at a
    # time; pids in a sync batch are few)
    for pid in pids:
        _set_root_and_state(conn, pid)


def _set_root_and_state(conn, pid) -> None:
    """Set root_uid, is_root and grouping_state from resolved parent refs.

    Root candidates = members with no resolved parent ref. Exactly one
    candidate of role 'original' with no unresolved refs -> clean; one
    candidate otherwise -> provisional; zero or several -> ambiguous.
    """
    members = conn.execute(
        "SELECT pa.uid, pa.role, pa.has_parent_refs, a.start_date, "
        "       a.description, a.authority "
        "FROM project_applications pa JOIN applications a ON a.uid = pa.uid "
        "WHERE pa.project_id = %s", (pid,)).fetchall()
    if not members:
        return

    # resolve each member's refs authority-wide in one query per authority
    cited_by_auth = defaultdict(set)
    for uid, _role, _hpr, _sd, desc, auth in members:
        cited_by_auth[auth].update(parent_refs_of(desc))
    resolved = {}
    for auth, refs in cited_by_auth.items():
        if refs:
            for ref, uid in conn.execute(
                    "SELECT cand.ref, a.uid FROM unnest(%s::text[]) AS cand(ref) "
                    "JOIN applications a ON a.authority = %s "
                    "  AND (upper(a.reference) = cand.ref "
                    "       OR starts_with(upper(a.reference), cand.ref || '/')) "
                    "ORDER BY cand.ref, a.start_date ASC NULLS LAST",
                    (list(refs), auth)):
                resolved.setdefault((auth, ref), uid)

    # Root candidates = members citing no ref that resolves to a stored
    # application. A no-ref discharge/variation/nma is NOT a candidate — it
    # belongs to the scheme but isn't an origin; only role 'original' members
    # (or, failing that, any member) with no resolved parent can be the root.
    no_parent = []  # members with no resolved parent ref
    has_unresolved = False
    for uid, role, _hpr, sd, desc, auth in members:
        refs = parent_refs_of(desc)
        resolved_refs = [r for r in refs if (auth, r) in resolved]
        has_unresolved = has_unresolved or any(
            r not in {x for x in refs if (auth, x) in resolved} for r in refs)
        if not resolved_refs:
            no_parent.append((uid, role, sd))

    # prefer original candidates; fall back to any no-parent member (e.g. a
    # reference cycle of discharges), else earliest member overall
    candidates = [c for c in no_parent if c[1] == "original"] or no_parent \
        or [(m[0], m[1], m[3]) for m in members]
    if len(candidates) == 1:
        root_uid, root_role, _ = candidates[0]
        state = ("clean" if root_role == "original" and not has_unresolved
                 else "provisional")
    else:
        # multiple candidate origins (or a cycle of originals) -> ambiguous
        state = "ambiguous"
    candidates.sort(key=lambda r: (r[2] is None, r[2], r[0]))
    root_uid = candidates[0][0]
    conn.execute("UPDATE projects SET root_uid = %s, grouping_state = %s "
                 "WHERE id = %s", (root_uid, state, pid))
    conn.execute("UPDATE project_applications SET is_root = (uid = %s) "
                 "WHERE project_id = %s", (root_uid, pid))


def drain_regroup_pending(conn) -> int:
    """Re-derive root/grouping_state for projects queued by the
    apply_grouping_resolution RPC after a manual assignment. Returns how many
    were processed. Idempotent; safe to call every run.
    """
    pids = [r[0] for r in conn.execute(
        "SELECT project_id FROM regroup_pending ORDER BY queued_at").fetchall()]
    for pid in pids:
        _set_root_and_state(conn, pid)
        conn.execute("DELETE FROM regroup_pending WHERE project_id = %s", (pid,))
    if pids:
        log.info("Re-derived root/state for %s manually-assigned project(s)",
                 len(pids))
    return len(pids)


_PC_RE = re.compile(r"\b[a-z]{1,2}\d[a-z\d]?\s*\d[a-z]{2}\b", re.I)
_STOP = {"and", "the", "of", "london", "barking", "dagenham", "road", "rd",
         "street", "st", "avenue", "ave", "lane", "close", "way", "court",
         "gardens", "house", "lodge"}


def _pc(x) -> str:
    return re.sub(r"\s", "", (x or "").upper())


def _addr(a) -> str:
    return _PC_RE.sub(" ", a or "").lower()


def _nums(a) -> set[str]:
    return set(re.findall(r"\b\d+[a-z]?\b", _addr(a)))


def _words(a) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", _addr(a))) - _STOP


def _street_ok(a, b) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def link_rule(c_addr, c_pc, p_addr, p_pc) -> str | None:
    """Which corroboration rule (if any) says child and parent are one site.
    A: same postcode. B: a postcode is missing, street words overlap and house
    numbers don't conflict. X2: postcodes differ but house number and street
    match. Anything else is a cross-reference and stays separate."""
    a, b = _pc(c_pc), _pc(p_pc)
    if a and b:
        if a == b:
            return "A"
        na, nb = _nums(c_addr), _nums(p_addr)
        return "X2" if na and nb and na & nb and _street_ok(c_addr, p_addr) else None
    if _street_ok(c_addr, p_addr):
        na, nb = _nums(c_addr), _nums(p_addr)
        if not na or not nb or na & nb:
            return "B"
    return None


def merge_linked(conn, pids=None, dry_run: bool = False) -> dict:
    """Merge projects linked by a corroborated child -> parent_ref edge.

    pids limits the scan to edges whose child project is in pids (None = all).
    The survivor of each group is its largest project (lowest id on ties); the
    absorbed address keys become aliases so later regroups land in the survivor.
    """
    params: tuple = ()
    where = ""
    if pids is not None:
        if not pids:
            return {}
        where = "WHERE pa.project_id = ANY(%s)"
        params = (list(pids),)
    rows = conn.execute(
        "SELECT DISTINCT pa.project_id, pb.project_id, pa.parent_ref, "
        "ch.address, ch.postcode, par.address, par.postcode "
        "FROM project_applications pa "
        "JOIN applications ch ON ch.uid = pa.uid "
        "JOIN applications par ON par.authority = ch.authority "
        "  AND (upper(par.reference) = pa.parent_ref "
        "       OR starts_with(upper(par.reference), pa.parent_ref || '/')) "
        "JOIN project_applications pb ON pb.uid = par.uid "
        "  AND pb.project_id <> pa.project_id " + where, params).fetchall()

    stats: dict = {"links": len(rows)}
    edges = set()
    unmerged = set()
    for cp, pp, ref, ca, cpc, pa_, ppc in rows:
        rule = link_rule(ca, cpc, pa_, ppc)
        if rule:
            stats[rule] = stats.get(rule, 0) + 1
            edges.add((cp, pp))
        else:
            unmerged.add((cp, ref, pp))
    for cp, ref, pp in sorted(unmerged):
        log.info("cross-reference, not merged: project %s cites %s (project %s)",
                 cp, ref, pp)
    stats["not_merged"] = len(unmerged)

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups: dict[int, set[int]] = {}
    for x in list(parent):
        groups.setdefault(find(x), set()).add(x)
    groups = {r: g for r, g in groups.items() if len(g) > 1}
    stats["groups"] = len(groups)
    stats["absorbed"] = sum(len(g) - 1 for g in groups.values())
    stats["largest"] = max((len(g) for g in groups.values()), default=0)
    if dry_run or not groups:
        return stats

    sizes = dict(conn.execute(
        "SELECT id, n_applications FROM projects WHERE id = ANY(%s)",
        (sorted({p for g in groups.values() for p in g}),)).fetchall())
    survivors = []
    for g in groups.values():
        keep = max(g, key=lambda p: (sizes.get(p, 0), -p))
        gone = sorted(g - {keep})
        conn.execute(
            "INSERT INTO project_aliases (authority, address_key, project_id) "
            "SELECT authority, address_key, %s FROM projects WHERE id = ANY(%s) "
            "ON CONFLICT (authority, address_key) DO UPDATE "
            "SET project_id = excluded.project_id", (keep, gone))
        conn.execute(
            "UPDATE project_aliases SET project_id = %s WHERE project_id = ANY(%s)",
            (keep, gone))
        conn.execute(
            "INSERT INTO project_applications (project_id, uid, parent_ref, role, "
            "has_parent_refs) SELECT %s, uid, parent_ref, role, has_parent_refs "
            "FROM project_applications WHERE project_id = ANY(%s) "
            "ON CONFLICT (project_id, uid) DO NOTHING", (keep, gone))
        conn.execute("UPDATE missing_parents SET project_id = %s "
                     "WHERE project_id = ANY(%s)", (keep, gone))
        conn.execute("DELETE FROM projects WHERE id = ANY(%s)", (gone,))
        survivors.append(keep)
    refresh(conn, survivors)
    log.info("Merged %s projects into %s", stats["absorbed"], len(groups))
    return stats
