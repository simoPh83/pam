"""Group applications into projects (development sites).

One physical development produces many applications (original permission,
s73 variations, condition discharges, NMAs). Outreach is per project, not per
application. Clustering is by (authority, normalised address); parent
references extracted from descriptions are kept for debugging/merges.

See docs/2026-10-04-project-grouping-spec.md.
"""

from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS manual_parent_refs (
    uid         TEXT PRIMARY KEY REFERENCES applications(uid) ON DELETE CASCADE,
    authority   TEXT NOT NULL,
    reference   TEXT NOT NULL,          -- user-supplied parent ref (upper-case)
    created_by  UUID,                   -- auth.uid() of the resolving user
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Columns added after first release — applied idempotently by ensure_schema().
MIGRATIONS = (
    "ALTER TABLE project_applications ADD COLUMN IF NOT EXISTS "
    "has_parent_refs BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS root_in_db BOOLEAN",
    # user-set role (pam-client "make singleton"); group() never re-derives it
    "ALTER TABLE project_applications ADD COLUMN IF NOT EXISTS "
    "role_override TEXT",
    # live chain-incomplete flag, derived with grouping_state in
    # _set_root_and_state(); root_in_db is kept as its negation (legacy name —
    # see docs/2026-10-14-backend-issue-root-in-db-vs-grouping-state.md)
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS "
    "has_missing_ancestors BOOLEAN",
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
    r"[.:]?\s*"  # tolerate "application: 22/03720/FUL"
    r"(?:ref(?:erence)?[.:]?\s*(?:no[.:]?\s*)?|no[.:]?\s*|\(\s*)?"
    r"([A-Z0-9][A-Z0-9/_.-]{4,25}\d[A-Z0-9/_.-]*)",
    re.I,
)

# "condition(s) <list> of <REF>" — a condition discharge that cites the parent
# by reference after the condition number(s), e.g. "contrary to condition 3 of
# PP/00/02566". Narrow (authority-style ref shapes only) so the condition list
# itself is never captured. Kept separate from PARENT_RE: widening the main
# pattern to tolerate the intervening number list caused large regressions.
# The list tolerates digits, separators, "(a)" sub-items and the word "and"
# ("condition 02, 03 and 04 of application 22/03720/FUL").
# General reference shape: 1-6 alnum first segment then up to 3 further
# '/'-joined segments, with a digit. The first segment allows 6 chars for
# legacy numeric refs (Westminster cites its 1960s permissions as
# "101822/C"). Tolerates Merton NN/PNNNN, Kensington PP/NN/NNNNN,
# Camden NNNN/NNNN/L, etc.
_REF_SHAPE = r"([A-Z0-9]{1,6}(?:/[A-Z0-9]{1,6}){1,3})"

CONDITION_OF_RE = re.compile(
    r"conditions?\s+(?:[\d,\s&]|\([^)]*\)|\band\b)+?of\s+"
    r"(?:planning\s+)?(?:permission|application|consent|ref(?:erence)?[.:]?|no[.:]?)?\s*"
    + _REF_SHAPE,
    re.I,
)

# "(varied|amended|superseded|replaced) by <REF>" — a follow-up citing the
# earlier application it modifies, e.g. "Ref: 22/00516/FUL (varied by
# 24/03476/VAR)".
VARIED_BY_RE = re.compile(
    r"(?:varied|amended|superseded|replaced)\s+by\s+"
    r"(?:planning\s+)?(?:permission|application|consent|ref(?:erence)?[.:]?)?\s*"
    r"([A-Z0-9][A-Z0-9/_.-]{4,25}\d[A-Z0-9/_.-]*)",
    re.I,
)

# Bare "Ref:"/"RN" citation prefixes with no leading keyword, e.g. "reserved
# matters approved under Ref: 23/02239/OUT", "(RN 24/05892/FULL)" or
# "planning permission dated 22/05/2025 ref 2025/1231". The left guard is a
# lookbehind (not \b) so the no-space Westminster house style "(RN25/02937/
# FULL)" matches while "RN" inside a longer alnum run (e.g. "MODERN25/1234")
# does not. Kept separate from PARENT_RE (no keyword context), so junk is
# filtered by _plausible_ref: the capture must be 6+ chars with a digit and a
# '/', and bare dates like 12/05/2023 are excluded.
REF_PREFIX_RE = re.compile(
    r"(?<![A-Z0-9])(?:refs?|RN)[.:]?\s*" + _REF_SHAPE,
    re.I,
)

# "permission [dated <date>] <REF>" — a citation where the decision date sits
# between the keyword and the reference, e.g. "planning permission dated
# 2 September 2022 PP/21/06400" or "…dated 1st October 2024 (24/00152/FULL)".
# The date tolerates ordinal suffixes (1st/16th) and the ref may be wrapped
# in bare parentheses. The optional date is bounded so plain "permission
# dated 2020" (no ref) never matches; junk shapes are filtered by
# _plausible_ref.
DATED_REF_RE = re.compile(
    r"(?:permission|approval|application|consent)\s+"
    r"(?:dated\s+(?:\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+)?\d{4}\s*)"
    r"(?:ref(?:erence)?[.:]?\s*|no[.:]?\s*)?"
    r"\(?" + _REF_SHAPE,
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


# Building-services usages role_of must ignore: "air condition(ing) unit"
# is not a planning condition; "discharge flue"/"discharge of extracted air"
# is not a condition discharge.
_BUILDING_SERVICES_RE = re.compile(
    r"\bair[- ]condition(?:ing|ers?|ed)?s?\b"
    r"|\bdischarge\s+(?:of\s+extracted\s+air|flues?|stacks?|grilles?|pipes?"
    r"|terminals?|points?|outlets?|vents?)\b",
    re.I,
)


def role_of(description: str | None, app_type: str | None = None,
            authority: str | None = None) -> str:
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
    # Building-services phrasing that is NOT a planning keyword:
    # "air condition unit" is not a planning condition; a "discharge flue" /
    # "discharge of extracted air" is not a condition discharge.
    text = _BUILDING_SERVICES_RE.sub(" ", text)
    is_var = ("variation of condition" in text or "s73" in text
              or "section 73" in text)
    is_nma = ("non-material amendment" in text or "non material amendment" in text
              or "s96a" in text or "section 96a" in text)
    is_dis = ("discharge" in text or "approval of details" in text
              or re.search(r"\bcondition\b", text))
    if parent_refs_of(description, authority):
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
    other regex false positives) and for pre-2000 years (PlanIt's historical
    coverage starts 2000–2002 depending on the authority). The parent hunt
    uses this only to order its queue, recent first — refs with no parseable
    year are still looked up (id_match needs no date window)."""
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


# Authorities whose references have no '/' (invisible to the generic
# patterns). Each maps to a narrow citation pattern applied only after a
# keyword, and only within that authority — the formats are too generic
# (bare digits / dotted) to match safely anywhere else.
_AUTH_REF_PATTERNS = {
    "Waltham Forest": r"(\d{6})\b",                 # 203833
    "Ealing":         r"(\d{6}[A-Z]{2,5})\b",       # 253454PALHE
    "Havering":       r"([A-Z]{1,3}\d{3,5}\.\d{2})\b",  # P1152.26
}
# Citation context for the no-slash authority formats: a keyword, an optional
# condition-number list (as in "condition 4 of ref 234842FUL"), then an
# optional label/date. Mirrors the generic patterns' contexts.
_AUTH_KEYWORD = (
    r"(?:permission|application|consent|approval|referenced|reference|condition)\s*"
    r"[\d,\s&]*?(?:\([^)]*\))?\s*(?:of\s+)?"
    r"(?:ref(?:erence)?[.:]?\s*|no[.:]?\s*)?(?:dated\s+[\d/]+\s*)?"
)


def parent_refs_of(description: str | None, authority: str | None = None) -> list[str]:
    """All planning references mentioned in a description (deduped, ordered).

    The PlanIt feed occasionally corrupts punctuation/quotes to a literal '?'
    (e.g. "condition?8?...of planning?permission?PP/22/06589"); normalise
    those to spaces before matching so the keyword context still parses.
    `authority` enables the no-slash per-authority formats above.
    """
    text = (description or "").replace("?", " ")
    refs, seen = [], set()
    for rx in (PARENT_RE, CONDITION_OF_RE, DATED_REF_RE, VARIED_BY_RE,
               REF_PREFIX_RE):
        for m in rx.finditer(text):
            ref = m.group(1).upper().rstrip(".,);")
            if _plausible_ref(ref) and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    auth_pat = _AUTH_REF_PATTERNS.get(authority or "")
    if auth_pat:
        for m in re.finditer(_AUTH_KEYWORD + auth_pat, text, re.I):
            ref = m.group(1).upper().rstrip(".,);")
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def _plausible_ref(ref: str) -> bool:
    """Cheap junk filter: >=6 chars, contains a digit and a '/', and is not a
    bare date like 12/05/2023. Real validation happens at resolution time
    (a candidate only links if it matches a stored application)."""
    return (len(ref) >= 6 and any(ch.isdigit() for ch in ref)
            and "/" in ref
            and not re.fullmatch(r"\d{2}/\d{2}/\d{4}", ref))


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


def _retire_projects(conn, pids, succ_of: dict | None = None) -> set:
    """Delete the given empty projects (no members), re-pointing their
    dependents to the project that absorbed their last members.

    succ_of maps a retired id to its absorbing project when the caller knows
    it (group() moves, the dual-membership repair); otherwise the successor
    falls back to another live project with the same (authority,
    address_key).

    Followers: missing_parents rows that still point at the retired id,
    project_aliases (its key belonged to the absorbed members), and the
    UI-owned tables (ui_project_stars, ui_project_meta, ui_project_practices,
    ui_project_agent_presence, ui_project_root_tsv) — same successor rule as
    the migration: the absorbed component is the least surprising default.
    grouping_review rows are left alone: pending multi_root rows are resolved
    by the cascade refresh of the successor and survivors; address-keyed
    unlinked rows (project_id NULL) are unaffected.

    Returns the ids actually retired (a pid with remaining members is kept).
    """
    pids = {int(p) for p in pids if p is not None}
    if not pids:
        return set()
    empty = {r[0] for r in conn.execute(
        "SELECT p.id FROM projects p WHERE p.id = ANY(%s) AND NOT EXISTS ("
        "  SELECT 1 FROM project_applications pa WHERE pa.project_id = p.id)",
        (sorted(pids),)).fetchall()}
    if not empty:
        return set()
    succ_of = succ_of or {}
    for pid in sorted(empty):
        succ = succ_of.get(pid)
        if succ is None:
            row = conn.execute(
                "SELECT p2.id FROM projects p2 "
                "JOIN projects p1 ON p1.id = %s "
                "WHERE p2.authority = p1.authority "
                "  AND p2.address_key = p1.address_key "
                "  AND p2.id <> p1.id AND p2.n_applications > 0 "
                "ORDER BY p2.n_applications DESC, p2.id LIMIT 1",
                (pid,)).fetchone()
            succ = row[0] if row else None
        if succ:
            conn.execute("UPDATE missing_parents SET project_id = %s "
                         "WHERE project_id = %s", (succ, pid))
            conn.execute("UPDATE project_aliases SET project_id = %s "
                         "WHERE project_id = %s", (succ, pid))
            # UI rows follow to the successor, deduped on each table's
            # natural key (mirrors the migration's repoint())
            conn.execute(
                "DELETE FROM ui_project_stars t WHERE t.project_id = %s "
                "AND EXISTS (SELECT 1 FROM ui_project_stars x "
                "  WHERE x.project_id = %s AND x.user_id = t.user_id)",
                (pid, succ))
            conn.execute("UPDATE ui_project_stars SET project_id = %s "
                         "WHERE project_id = %s", (succ, pid))
            conn.execute(
                "DELETE FROM ui_project_meta t WHERE t.project_id = %s "
                "AND EXISTS (SELECT 1 FROM ui_project_meta x "
                "  WHERE x.project_id = %s AND x.user_id = t.user_id)",
                (pid, succ))
            conn.execute("UPDATE ui_project_meta SET project_id = %s "
                         "WHERE project_id = %s", (succ, pid))
            conn.execute(
                "DELETE FROM ui_project_practices t WHERE t.project_id = %s "
                "AND EXISTS (SELECT 1 FROM ui_project_practices x "
                "  WHERE x.project_id = %s AND x.practice_id = t.practice_id)",
                (pid, succ))
            conn.execute("UPDATE ui_project_practices SET project_id = %s "
                         "WHERE project_id = %s", (succ, pid))
            for t in ("ui_project_agent_presence", "ui_project_root_tsv"):
                conn.execute(
                    f"DELETE FROM {t} t WHERE t.project_id = %s AND EXISTS ("
                    f"  SELECT 1 FROM {t} x WHERE x.project_id = %s)",
                    (pid, succ))
                conn.execute(f"UPDATE {t} SET project_id = %s "
                             f"WHERE project_id = %s", (succ, pid))
            log.info("Retired empty project %s (dependents -> %s)", pid, succ)
        else:
            # orphan id: nothing points at it; dependents keep a dangling or
            # cascaded reference exactly as a plain DELETE would leave them
            log.info("Retired empty project %s (no successor found)", pid)
        conn.execute("DELETE FROM projects WHERE id = %s", (pid,))
    return empty


def _manual_refs(conn, uids) -> dict[str, list[str]]:
    """User-supplied parent refs (pam-client manual-resolution dialog),
    keyed by uid. Unioned with the description-parsed refs everywhere refs
    are derived, so a manual link survives re-parses and re-groups."""
    out: dict[str, list[str]] = {}
    for uid, ref in conn.execute(
            "SELECT uid, reference FROM manual_parent_refs "
            "WHERE uid = ANY(%s)", (list(uids),)).fetchall():
        out.setdefault(uid, []).append(ref)
    return out


def group(uids, conn) -> int:
    """(Re)group the given application uids. Returns how many were grouped."""
    uids = list(uids)
    if not uids:
        return 0
    rows = conn.execute(
        "SELECT uid, authority, address, postcode, description, app_type "
        "FROM applications WHERE uid = ANY(%s)", (uids,)).fetchall()
    manual_refs = _manual_refs(conn, [r[0] for r in rows])
    role_overrides = dict(conn.execute(
        "SELECT uid, role_override FROM project_applications "
        "WHERE uid = ANY(%s) AND role_override IS NOT NULL",
        ([r[0] for r in rows],)).fetchall())

    # Pass 1: normalise in memory
    prepared = {}  # (authority, address_key) -> list of (uid, name, role, refs)
    all_refs: dict[str, set[str]] = {}  # authority -> refs
    for uid, authority, address, postcode, description, app_type in rows:
        key = address_key(address, postcode)
        if not key or not authority:
            continue
        refs = parent_refs_of(description, authority)
        if uid in manual_refs:
            refs = list(dict.fromkeys(refs + manual_refs[uid]))
        prepared.setdefault((authority, key), []).append(
            (uid, (address or "").strip()[:100],
             role_overrides.get(uid)
             or role_of(description, app_type, authority), refs))
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

    # current membership (unique index project_applications_uid_key
    # guarantees one row per uid) — manual singletons stay put
    cur_pid = dict(conn.execute(
        "SELECT uid, project_id FROM project_applications "
        "WHERE uid = ANY(%s)", (uids,)).fetchall())

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
        # a user-marked singleton (role_override) is never relocated into an
        # address bucket; only a resolved reference can move it
        if uid in role_overrides and uid in cur_pid:
            return cur_pid[uid]
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

    # Pass 4: move memberships. A uid belongs to exactly one project; the
    # upsert can't see rows under other project ids, so delete them first.
    # (Without this, a uid whose refs resolve after a singleton was created
    # ended up in both projects — see
    # docs/2026-10-12-backend-issue-dual-membership.md.)
    memberships = []
    for (auth, key), members in prepared.items():
        for uid, _name, role, refs in members:
            pid = target_project(auth, key, uid, refs)
            known = [r for r in resolved.get(auth, {}) if r in refs]
            memberships.append((pid, uid, known[0] if known else
                                (refs[0] if refs else None), role, bool(refs)))
    new_pid_of_uid = {uid: pid for pid, uid, *_ in memberships}
    old_pids = {}  # uid -> pids it belonged to (excluding the new one)
    for uid, pid in conn.execute(
            "SELECT uid, project_id FROM project_applications "
            "WHERE uid = ANY(%s)", (list(new_pid_of_uid),)).fetchall():
        if pid != new_pid_of_uid[uid]:
            old_pids.setdefault(uid, set()).add(pid)
    if old_pids:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE _moves (uid text, new_pid bigint, "
                "old_pid bigint) ON COMMIT DROP")
            with cur.copy("COPY _moves FROM STDIN") as cp:
                for uid, pids in old_pids.items():
                    for pid in pids:
                        cp.write_row((uid, new_pid_of_uid[uid], pid))
            cur.execute(
                "DELETE FROM project_applications pa USING _moves m "
                "WHERE pa.uid = m.uid AND pa.project_id = m.old_pid")
            # missing_parents / review rows follow the citing uid
            cur.execute(
                "UPDATE missing_parents mp SET project_id = m.new_pid "
                "FROM _moves m WHERE mp.requested_by = m.uid "
                "AND mp.project_id = m.old_pid")
            cur.execute(
                "UPDATE grouping_review g SET project_id = m.new_pid "
                "FROM _moves m WHERE g.uid = m.uid "
                "AND g.project_id = m.old_pid AND g.status = 'pending'")
            cur.execute("DROP TABLE _moves")  # group() is called per batch
                                              # within one transaction
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO project_applications (project_id, uid, parent_ref, role, "
            "has_parent_refs) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (project_id, uid) DO UPDATE "
            "SET parent_ref = excluded.parent_ref, role = excluded.role, "
            "has_parent_refs = excluded.has_parent_refs",
            memberships)
    # Re-derive (and retire) the projects the uids were moved away from;
    # they join the refresh pass below.
    abandoned = {pid for pids in old_pids.values() for pid in pids}
    succ_of = {old: new_pid_of_uid[uid] for uid, pids in old_pids.items()
               for old in pids}
    retired = _retire_projects(conn, abandoned, succ_of)
    touched = abandoned - retired

    # Pass 4c: a uid that now yields parsed refs no longer belongs in the
    # unlinked review queue (queued when its description yielded none —
    # parser improvements can change that). If the refs resolve it joined a
    # scheme; if not, missing_parents (pass 4b) owns the follow-up.
    uids_now_linked = [uid for _pid, uid, _pr, _role, hpr in memberships
                       if hpr]
    if uids_now_linked:
        conn.execute(
            "UPDATE grouping_review SET status = 'resolved', "
            "resolution = 'obsolete', resolved_at = now() "
            "WHERE uid = ANY(%s) AND reason LIKE 'unlinked%%' "
            "AND status = 'pending'", (uids_now_linked,))

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

    # Pass 5: refresh aggregates + root/state for touched projects (targets,
    # plus the still-populated projects memberships were moved away from)
    pids = sorted({pid for pid, *_ in memberships} | touched)
    refresh(conn, pids)

    grouped = len(memberships)
    log.info("Grouped %s applications into %s projects", grouped, len(pids))
    return grouped


def refresh(conn, pids) -> None:
    """Recompute aggregates and root for the given projects.

    Aggregates (n_applications, latest_uid, name, first_seen, last_updated)
    come from set-based SQL over members. root_uid and grouping_state follow
    the reference-component structure: the root is the member with no
    *resolved* parent ref; multiple candidate roots mark the project
    ambiguous. A project with no unresolved refs anywhere is clean. The chain
    flags (root_in_db, has_missing_ancestors) are derived live alongside
    grouping_state in _set_root_and_state(), which also enqueues any
    unresolved refs it finds to missing_parents — so the hunt queue cannot
    drift behind the flags when refresh() runs without a group() pass
    (parser updates, parents purged after a clean compute).
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
    # Self-heal the hunt queue: drop missing_parents rows whose ref now
    # resolves in applications (same rule as the hunt's local-first lookup).
    # The hunt only deletes rows it looks up itself, so a parent arriving via
    # ordinary sync/backfill would otherwise leave a stale row behind.
    # Scoped to rows tied to the refreshed projects. The chain flags no
    # longer read missing_parents — this is queue hygiene only.
    conn.execute(
        "DELETE FROM missing_parents mp "
        "WHERE EXISTS (SELECT 1 FROM applications a "
        "  WHERE a.authority = mp.authority "
        "    AND (upper(a.reference) = mp.reference "
        "         OR starts_with(upper(a.reference), mp.reference || '/'))) "
        "AND (mp.project_id = ANY(%s) "
        "     OR mp.requested_by IN (SELECT pa.uid FROM project_applications pa "
        "                            WHERE pa.project_id = ANY(%s)) "
        "     OR EXISTS (SELECT 1 FROM project_applications pa "
        "                JOIN projects p ON p.id = pa.project_id "
        "                WHERE pa.project_id = ANY(%s) "
        "                  AND p.authority = mp.authority "
        "                  AND pa.parent_ref = mp.reference))",
        (pids, pids, pids))

    # root + grouping_state from the reference structure (one project at a
    # time; pids in a sync batch are few)
    for pid in pids:
        _set_root_and_state(conn, pid)


def _set_root_and_state(conn, pid) -> None:
    """Set root_uid, is_root, grouping_state and the chain flags.

    Root candidates = members with no resolved parent ref. Exactly one
    candidate of role 'original' with no unresolved refs -> clean; one
    candidate otherwise -> provisional; several -> ambiguous. Two
    no-candidate shapes are NOT ambiguous: if every parentless member cites
    only unresolved refs the project is awaiting the parent hunt (earliest
    citer elected as provisional root); if every member cites a ref that
    resolves — a reference cycle (e.g. a Full permission and its
    listed-building consent citing each other as siblings) or a fragment
    whose chain head lives in another project — the earliest member is
    elected as provisional root. Neither shape raises a multi_root review.

    Chain flags, both derived here from the same live re-resolution so they
    cannot diverge: has_missing_ancestors = any member cites a ref that does
    not resolve to a stored application; root_in_db is its negation (legacy
    name kept for the existing client contract — it never meant "the
    root_uid row exists"; the root is always elected from stored members).
    """
    members = conn.execute(
        "SELECT pa.uid, pa.role, pa.has_parent_refs, a.start_date, "
        "       a.description, a.authority, a.app_type "
        "FROM project_applications pa JOIN applications a ON a.uid = pa.uid "
        "WHERE pa.project_id = %s", (pid,)).fetchall()
    if not members:
        return

    manual_refs = _manual_refs(conn, [m[0] for m in members])

    def refs_of(uid: str, desc: str, auth: str) -> list[str]:
        refs = parent_refs_of(desc, auth)
        if uid in manual_refs:
            refs = list(dict.fromkeys(refs + manual_refs[uid]))
        return refs

    # resolve each member's refs authority-wide in one query per authority
    member_refs = {}  # uid -> parsed + manual parent refs
    cited_by_auth = defaultdict(set)
    for uid, _role, _hpr, _sd, desc, auth, _at in members:
        member_refs[uid] = refs_of(uid, desc, auth)
        cited_by_auth[auth].update(member_refs[uid])
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

    # Keep the hunt queue complete: refs that parse but don't resolve must
    # reach missing_parents from here too, not only from group() pass 4b.
    # group() runs when membership is recomputed, so projects grouped by an
    # older parser (or whose parent was purged after a clean compute) would
    # otherwise never be hunted and their flags could never heal.
    # ON CONFLICT preserves the first enqueue and any 'exhausted' verdict.
    unresolved = {}  # (authority, ref) -> a citing member uid
    for uid, _role, _hpr, _sd, _desc, auth, _at in members:
        for ref in member_refs[uid]:
            if (auth, ref) not in resolved:
                unresolved.setdefault((auth, ref), uid)
    if unresolved:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO missing_parents "
                "(authority, reference, ref_year, requested_by, project_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (authority, reference) DO NOTHING",
                [(auth, ref, ref_year_of(ref), uid, pid)
                 for (auth, ref), uid in unresolved.items()])

    # Root candidates = members citing no ref that resolves to a stored
    # application. A no-ref discharge/variation/nma is NOT a candidate — it
    # belongs to the scheme but isn't an origin; only role 'original' members
    # (or, failing that, any member) with no resolved parent can be the root.
    no_parent = []  # members with no resolved parent ref
    has_unresolved = False
    for uid, role, _hpr, sd, desc, auth, _at in members:
        refs = member_refs[uid]
        resolved_refs = [r for r in refs if (auth, r) in resolved]
        has_unresolved = has_unresolved or any(
            r not in {x for x in refs if (auth, x) in resolved} for r in refs)
        if not resolved_refs:
            no_parent.append((uid, role, sd))

    # Prefer original candidates. Failing that, a project whose parentless
    # members all cite (unresolved) refs is awaiting the parent hunt, not
    # ambiguous: elect the earliest citer as provisional root and raise no
    # multi_root review — the hunt re-forms the structure when a parent
    # arrives, and an exhausted hunt leaves a truthful provisional chain
    # instead of N pseudo-candidate children of the same missing parent.
    originals = [c for c in no_parent if c[1] == "original"]
    if originals:
        candidates = originals
    elif no_parent and all(member_refs[uid] for uid, _r, _s in no_parent):
        candidates = [min(no_parent, key=lambda r: (r[2] is None, r[2], r[0]))]
    elif not no_parent:
        # Reference cycle (or a fragment whose chain head lives in another
        # project): every member cites a ref that resolves, so no member is
        # a root candidate. Not multi-origin competition — elect the
        # earliest member as provisional root and raise no multi_root
        # review (its candidates would be the empty no_parent list).
        candidates = [min(((m[0], m[1], m[3]) for m in members),
                          key=lambda r: (r[2] is None, r[2], r[0]))]
    else:
        # parentless blob citing nothing -> genuinely competing origins
        candidates = no_parent
    if len(candidates) == 1:
        root_uid, root_role, _ = candidates[0]
        state = ("clean" if root_role == "original" and not has_unresolved
                 else "provisional")
    else:
        # multiple candidate origins -> ambiguous
        state = "ambiguous"
    candidates.sort(key=lambda r: (r[2] is None, r[2], r[0]))
    root_uid = candidates[0][0]
    conn.execute("UPDATE projects SET root_uid = %s, grouping_state = %s, "
                 "has_missing_ancestors = %s, root_in_db = %s "
                 "WHERE id = %s",
                 (root_uid, state, has_unresolved, not has_unresolved, pid))
    conn.execute("UPDATE project_applications SET is_root = (uid = %s) "
                 "WHERE project_id = %s", (root_uid, pid))
    _sync_multi_root_review(conn, pid, members, no_parent, state)


def _sync_multi_root_review(conn, pid, members, no_parent, state) -> None:
    """Keep one pending multi_root review row iff the project is ambiguous.

    Ambiguous: insert a pending row if none exists, else refresh its
    candidates snapshot in place (memberships drift as grouping runs).
    Candidates are the members with no resolved parent ref (same set the
    migration's classify() used) — NOT just has_parent_refs=false, which
    would miss roots citing unresolvable refs.
    Leaving ambiguous: pending rows are resolved with resolution='obsolete'
    (no user action happened; resolved_by stays NULL).
    """
    if state != "ambiguous":
        conn.execute(
            "UPDATE grouping_review SET status = 'resolved', "
            "resolution = 'obsolete', resolved_at = now() "
            "WHERE reason = 'multi_root' AND project_id = %s "
            "AND status = 'pending'", (pid,))
        return
    by_uid = {m[0]: m for m in members}
    cands = []
    for uid, _role, sd in sorted(no_parent,
                                 key=lambda r: (r[2] is None, r[2], r[0])):
        m = by_uid[uid]
        cands.append({"uid": uid, "description": (m[4] or "")[:200],
                      "app_type": m[6],
                      "start_date": str(sd) if sd else None})
    payload = json.dumps(cands)
    row = conn.execute(
        "SELECT id FROM grouping_review WHERE reason = 'multi_root' "
        "AND project_id = %s AND status = 'pending'", (pid,)).fetchone()
    if row:
        conn.execute("UPDATE grouping_review SET candidates = %s::jsonb "
                     "WHERE id = %s", (payload, row[0]))
    else:
        conn.execute(
            "INSERT INTO grouping_review (authority, project_id, reason, "
            "candidates) VALUES (%s, %s, 'multi_root', %s::jsonb)",
            (members[0][5], pid, payload))


def split_project(conn, project_id: int) -> dict:
    """Split an ambiguous project into its reference-connected components.

    Triggered by apply_grouping_resolution(..., 'split') which enqueues a
    kind='split' job; the worker calls this (pure local DB work — refs are
    resolved against the applications table only, no PlanIt calls).

    The component containing the project's current root_uid keeps the
    existing projects.id; every other component becomes a fresh projects row.
    missing_parents rows follow the component containing their requested_by
    uid and aliases follow their component's address keys. UI-owned tables
    (ui_project_*) stay on the retained id — we can't know which scheme the
    user cared about; the old root's component is the least surprising
    default. linked_by is untouched: the user's decision is recorded on the
    review row, not on memberships.

    One transaction; the projects row is locked FOR UPDATE so a concurrent
    sync regroup can't interleave.
    """
    stats: dict = {"components": 0, "new_project_ids": [],
                   "moved_applications": 0}
    with conn.transaction():
        row = conn.execute(
            "SELECT id, authority, root_uid FROM projects WHERE id = %s "
            "FOR UPDATE", (project_id,)).fetchone()
        if not row:
            log.info("Split requested for deleted project %s", project_id)
            return {**stats, "note": "project_gone"}
        _pid, authority, root_uid = row
        req = conn.execute(
            "SELECT requested_by FROM split_requests WHERE project_id = %s",
            (project_id,)).fetchone()
        conn.execute(
            "UPDATE split_requests SET status = 'running' "
            "WHERE project_id = %s", (project_id,))

        members = conn.execute(
            "SELECT pa.uid, a.authority, a.description, a.address, a.postcode "
            "FROM project_applications pa JOIN applications a ON a.uid = pa.uid "
            "WHERE pa.project_id = %s", (project_id,)).fetchall()
        info = {m[0]: m[1:] for m in members}  # uid -> (auth, desc, addr, pc)

        # Re-parse refs and resolve authority-wide (exact + suffix-stripped,
        # same as group()), keeping only edges whose both ends are members.
        cited_by_auth: dict[str, set[str]] = defaultdict(set)
        parsed = {}
        for uid, (auth, desc, _addr, _pc) in info.items():
            refs = parent_refs_of(desc, auth)
            parsed[uid] = refs
            cited_by_auth[auth].update(refs)
        resolved = {}  # (authority, cited_ref) -> uid
        for auth, refs in cited_by_auth.items():
            if not refs:
                continue
            for ref, uid in conn.execute(
                    "SELECT cand.ref, a.uid FROM unnest(%s::text[]) AS cand(ref) "
                    "JOIN applications a ON a.authority = %s "
                    "  AND (upper(a.reference) = cand.ref "
                    "       OR starts_with(upper(a.reference), cand.ref || '/')) "
                    "ORDER BY cand.ref, a.start_date ASC NULLS LAST",
                    (list(refs), auth)):
                resolved.setdefault((auth, ref), uid)

        # undirected connected components (DSU) over the internal edges
        parent = {uid: uid for uid in info}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for uid, (auth, _d, _a, _p) in info.items():
            for ref in parsed[uid]:
                target = resolved.get((auth, ref))
                if target in info and target != uid:
                    ra, rb = find(uid), find(target)
                    if ra != rb:
                        parent[rb] = ra
        comps: dict[str, list[str]] = defaultdict(list)
        for uid in info:
            comps[find(uid)].append(uid)
        if root_uid in info:
            comps = sorted(comps.values(), key=lambda c: root_uid not in c)
        else:  # stale root_uid — retain the largest component
            comps = sorted(comps.values(), key=lambda c: -len(c))
        stats["components"] = len(comps)

        if len(comps) < 2:
            # Data changed since the request (e.g. a parent-hunt linked the
            # roots): nothing to split. Re-derive state — _set_root_and_state
            # resolves the review obsolete if the project left ambiguous, or
            # refreshes its candidates if it genuinely still is.
            _set_root_and_state(conn, project_id)
            conn.execute("DELETE FROM split_requests WHERE project_id = %s",
                         (project_id,))
            return {**stats, "note": "single_component_no_split"}

        # Close out the old review rows BEFORE re-deriving, so a component
        # that lands back in ambiguous gets a fresh pending row (Part 1)
        # rather than inheriting the one resolved here.
        conn.execute(
            "UPDATE grouping_review SET status = 'resolved', "
            "resolution = 'split', resolved_by = %s, resolved_at = now() "
            "WHERE reason = 'multi_root' AND project_id = %s "
            "AND status = 'pending'",
            (req[0] if req else None, project_id))

        retained_key = conn.execute(
            "SELECT address_key FROM projects WHERE id = %s",
            (project_id,)).fetchone()[0]
        new_ids = []
        for comp in comps[1:]:  # comps[0] contains the old root: retained
            addr_opts = [(info[u][2], info[u][3]) for u in comp
                         if info[u][2] and info[u][2].strip()]
            addr, pc = (min(addr_opts, key=lambda ap: len(ap[0]))
                        if addr_opts else (None, None))
            new_id = conn.execute(
                "INSERT INTO projects (authority, address_key, grouping_state) "
                "VALUES (%s, %s, 'provisional') RETURNING id",
                (authority, address_key(addr, pc))).fetchone()[0]
            new_ids.append(new_id)
            conn.execute(
                "UPDATE project_applications SET project_id = %s "
                "WHERE project_id = %s AND uid = ANY(%s)",
                (new_id, project_id, comp))
            conn.execute(
                "UPDATE missing_parents SET project_id = %s "
                "WHERE project_id = %s AND requested_by = ANY(%s)",
                (new_id, project_id, comp))
            comp_keys = {address_key(info[u][2], info[u][3]) for u in comp}
            comp_keys.discard(None)
            comp_keys.discard(retained_key)
            if comp_keys:
                conn.execute(
                    "UPDATE project_aliases SET project_id = %s "
                    "WHERE project_id = %s AND authority = %s "
                    "AND address_key = ANY(%s)",
                    (new_id, project_id, authority, sorted(comp_keys)))
        stats["new_project_ids"] = new_ids
        stats["moved_applications"] = sum(len(c) for c in comps[1:])

        refresh(conn, [project_id] + new_ids)
        conn.execute("DELETE FROM split_requests WHERE project_id = %s",
                     (project_id,))
    log.info("Split project %s into %s components (new ids %s, %s apps moved)",
             project_id, stats["components"], new_ids,
             stats["moved_applications"])
    return stats


def drain_regroup_pending(conn) -> int:
    """Re-derive aggregates and root/grouping_state for projects queued by
    the manual-resolution RPCs (apply_grouping_resolution,
    resolve_review_with_ref, resolve_review_singleton). Projects left
    memberless by a membership move are retired. Returns how many were
    processed. Idempotent; safe to call every run.
    """
    pids = [r[0] for r in conn.execute(
        "SELECT project_id FROM regroup_pending ORDER BY queued_at").fetchall()]
    if not pids:
        return 0
    refresh(conn, pids)  # aggregates + root/state (superset of
                         # _set_root_and_state); the membership moved, so
                         # n_applications etc. are stale too
    empty = {r[0] for r in conn.execute(
        "SELECT p.id FROM projects p WHERE p.id = ANY(%s) "
        "AND NOT EXISTS (SELECT 1 FROM project_applications pa "
        "                WHERE pa.project_id = p.id)", (pids,)).fetchall()}
    if empty:
        _retire_projects(conn, empty, {})
    conn.execute("DELETE FROM regroup_pending WHERE project_id = ANY(%s)",
                 (pids,))
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
