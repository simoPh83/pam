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
    PRIMARY KEY (project_id, uid)
);
CREATE INDEX IF NOT EXISTS idx_project_applications_uid ON project_applications (uid);
"""

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s/-]")
_UNIT = re.compile(r"\b(?:FLAT|UNIT|APARTMENT|ROOM|APPT)\s+\w+\s*,?\s*", re.I)
_POSTCODE = re.compile(r"\s+[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\s*$", re.I)

PARENT_RE = re.compile(
    r"(?:permission|application|consent|amendments?)\s*"
    r"(?:ref(?:erence)?[.:]?\s*|no[.:]?\s*|\(\s*)?"
    r"([A-Z0-9][A-Z0-9/_.-]{4,25}\d[A-Z0-9/_.-]*)",
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
    text = " ".join(t for t in (description, app_type) if t).lower()
    if ("variation of condition" in text or "s73" in text or "section 73" in text):
        return "variation"
    if ("non-material amendment" in text or "non material amendment" in text
            or "s96a" in text or "section 96a" in text):
        return "nma"
    if ("pursuant to" in text or "approval of details" in text
            or "discharge" in text or "condition" in text):
        return "discharge"
    return "original"


def parent_refs_of(description: str | None) -> list[str]:
    """All planning references mentioned in a description (deduped, ordered)."""
    refs, seen = [], set()
    for m in PARENT_RE.finditer(description or ""):
        ref = m.group(1).upper().rstrip(".,);")
        if len(ref) >= 6 and any(ch.isdigit() for ch in ref) and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _resolve_refs(refs: set[str], authority: str, conn) -> dict[str, str]:
    """Map each extracted ref to the ref whose application has the earliest
    start_date in this authority (self-mapped); refs with no match are absent."""
    if not refs:
        return {}
    return {r[0]: r[0] for r in conn.execute(
        "SELECT upper(reference) FROM applications "
        "WHERE authority = %s AND upper(reference) = ANY(%s) "
        "ORDER BY start_date ASC NULLS LAST", (authority, list(refs)))}


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

    # Pass 3: bulk-insert projects, then read back their ids
    keys = list(prepared)
    conn.execute(
        "INSERT INTO projects (authority, name, address_key) "
        "SELECT authority, min(name), address_key FROM ("
        "  SELECT * FROM unnest(%s::text[], %s::text[], %s::text[])"
        ") v(authority, name, address_key) "
        "GROUP BY authority, address_key "
        "ON CONFLICT (authority, address_key) DO NOTHING",
        ([k[0] for k in keys],
         [min((m[1] for m in prepared[k] if m[1]), default=None) for k in keys],
         [k[1] for k in keys]))
    id_rows = conn.execute(
        "SELECT p.authority, p.address_key, p.id FROM projects p "
        "JOIN (SELECT * FROM unnest(%s::text[], %s::text[])) v(authority, address_key) "
        "ON v.authority = p.authority AND v.address_key = p.address_key",
        ([k[0] for k in keys], [k[1] for k in keys])).fetchall()
    pid_of = {(a, k): pid for a, k, pid in id_rows}

    # Pass 4: bulk-upsert memberships
    memberships = []
    for (auth, key), members in prepared.items():
        pid = pid_of[(auth, key)]
        for uid, _name, role, refs in members:
            known = [r for r in resolved.get(auth, {}) if r in refs]
            memberships.append((pid, uid, known[0] if known else
                                (refs[0] if refs else None), role))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO project_applications (project_id, uid, parent_ref, role) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (project_id, uid) DO UPDATE "
            "SET parent_ref = excluded.parent_ref, role = excluded.role",
            memberships)

    # Pass 5: refresh aggregates for touched projects in one set-based pass
    pids = list(pid_of.values())
    conn.execute(
        """
        UPDATE projects p SET
            n_applications = s.n,
            root_uid       = s.root_uid,
            latest_uid     = s.latest_uid,
            name           = s.name,
            first_seen     = s.first_seen,
            last_updated   = s.last_updated
        FROM (
            SELECT pa.project_id,
                   count(*) AS n,
                   (array_agg(a.uid ORDER BY
                       (pa.role = 'original') DESC,
                       a.start_date ASC NULLS LAST, a.uid))[1] AS root_uid,
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
    conn.execute(
        "UPDATE project_applications pa SET is_root = (pa.uid = p.root_uid) "
        "FROM projects p WHERE p.id = pa.project_id AND p.id = ANY(%s)", (pids,))

    # Merge candidates: chosen parent_ref lives in a different project
    refs_used = sorted({ref for _pid, _uid, ref, _r in memberships if ref})
    if refs_used:
        other = {}
        for ref, pid in conn.execute(
                "SELECT upper(a.reference), pa.project_id "
                "FROM project_applications pa "
                "JOIN applications a ON a.uid = pa.uid "
                "WHERE upper(a.reference) = ANY(%s)", (refs_used,)):
            other.setdefault(ref, set()).add(pid)
        for pid, _uid, ref, _r in memberships:
            if ref and any(p != pid for p in other.get(ref, ())):
                log.info("merge candidate: project %s parent_ref %s also in "
                         "project %s", pid, ref, sorted(other[ref] - {pid}))

    grouped = len(memberships)
    log.info("Grouped %s applications into %s projects", grouped, len(pid_of))
    return grouped
