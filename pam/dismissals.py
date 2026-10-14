"""Dismissed-project tombstones: never re-ingest minor-work projects.

When a project is purged as minor (scripts/purge_minor_projects.py) or a
minor original is skipped at ingest (pam.main.execute), we record a minimal
tombstone so the scheme is never re-fetched:

- dismissed_refs: EVERY member reference, upper-cased — children cite the
  root, or a sibling child, or a variation of it, so all of them must catch
  future citations;
- dismissed_projects: the (authority, address_key) — catches citation-free
  children that would otherwise group by address.

A substantial signal always beats a tombstone at ingest: a new major
application at a previously dismissed address is stored and a fresh project
forms (the tombstone rows live in their own table, so projects'
UNIQUE(authority, address_key) is unaffected).

Tombstones carry the classifier `reason`, so relaxing a rule later means
deleting the matching rows (refs cascade) and letting syncs re-offer the
records.

Worker-owned tables: RLS enabled, no grants — pam-client never reads them.
"""

from __future__ import annotations

import logging

from .classify import is_minor, keep_reasons, minor_hits
from .projects import address_key, parent_refs_of

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dismissed_projects (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    authority       TEXT NOT NULL,
    address_key     TEXT,
    main_reference  TEXT,
    reason          TEXT NOT NULL,       -- classifier buckets, comma-joined
    n_members       INTEGER NOT NULL DEFAULT 1,
    dismissed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dismissed_projects_addr
    ON dismissed_projects (authority, address_key);
CREATE TABLE IF NOT EXISTS dismissed_refs (
    authority             TEXT NOT NULL,
    reference             TEXT NOT NULL,
    dismissed_project_id  BIGINT REFERENCES dismissed_projects(id)
                          ON DELETE CASCADE,
    PRIMARY KEY (authority, reference)
);
ALTER TABLE dismissed_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE dismissed_refs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON dismissed_projects FROM anon, authenticated;
REVOKE ALL ON dismissed_refs FROM anon, authenticated;
"""


def ensure_schema(conn) -> None:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)


class Dismissals:
    """In-memory view of the tombstones, for fast per-record ingest checks."""

    def __init__(self, refs: set[tuple[str, str]],
                 addrs: set[tuple[str, str]]) -> None:
        self.refs = refs      # (authority, UPPER reference)
        self.addrs = addrs    # (authority, address_key)

    @classmethod
    def load(cls, conn) -> "Dismissals":
        refs = {(a, r) for a, r in conn.execute(
            "SELECT authority, reference FROM dismissed_refs")}
        addrs = {(a, k) for a, k in conn.execute(
            "SELECT authority, address_key FROM dismissed_projects "
            "WHERE address_key IS NOT NULL")}
        log.info("Dismissals loaded: %d refs, %d addresses", len(refs), len(addrs))
        return cls(refs, addrs)

    def dismiss(self, conn, *, authority: str, address_key: str | None,
                references: list[str], reason: str, n_members: int = 1,
                main_reference: str | None = None) -> None:
        """Record a dismissal (DB + memory). Caller commits."""
        main_reference = main_reference or (references[0] if references else None)
        pid = conn.execute(
            "INSERT INTO dismissed_projects (authority, address_key, "
            "main_reference, reason, n_members) VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id",
            (authority, address_key, main_reference, reason, n_members),
        ).fetchone()[0]
        for ref in dict.fromkeys(references):
            conn.execute(
                "INSERT INTO dismissed_refs (authority, reference, "
                "dismissed_project_id) VALUES (%s, %s, %s) "
                "ON CONFLICT (authority, reference) DO NOTHING",
                (authority, ref, pid))
            self.refs.add((authority, ref))
        if address_key:
            self.addrs.add((authority, address_key))


def record_verdict(d: Dismissals, row: dict) -> tuple[str, str | None]:
    """Ingest decision for one normalized record.

    Returns ("store", None) or ("skip"/"dismiss", reason). Order:
      1. a keep signal always stores (re-opens dismissed addresses);
      2. citing a dismissed ref skips (the project was purged as minor);
      3. citation-free at a dismissed address skips;
      4. a citation-free minor original is dismissed (tombstoned) + skipped;
      5. anything else stores — follow-ups citing live/unknown parents are
         grouping glue; the project-level verdict governs them.
    """
    desc = row.get("description")
    authority = row.get("authority")
    hits = minor_hits(desc, row.get("app_type"))
    if keep_reasons(desc, row.get("app_size"), row.get("n_dwellings"), hits):
        return "store", None
    refs = parent_refs_of(desc, authority)
    if refs:
        if any((authority, r) in d.refs for r in refs):
            return "skip", "cites_dismissed"
        return "store", None
    akey = address_key(row.get("address"), row.get("postcode"))
    if akey and (authority, akey) in d.addrs:
        return "skip", "dismissed_address"
    if is_minor(desc, row.get("app_type"), row.get("app_size"),
                row.get("n_dwellings")):
        return "dismiss", ",".join(hits) or "minor"
    return "store", None


def dismiss_record(conn, d: Dismissals, row: dict, reason: str) -> None:
    """Tombstone a minor original skipped at ingest."""
    ref = (row.get("reference") or "").strip().upper()
    d.dismiss(conn, authority=row.get("authority"),
              address_key=address_key(row.get("address"), row.get("postcode")),
              references=[ref] if ref else [], reason=reason)
