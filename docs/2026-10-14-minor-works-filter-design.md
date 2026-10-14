# Minor-works filter + purge design (DB slimming)

**Date:** 2026-10-14
**Status:** RULES AGREED IN PRINCIPLE with product owner (extensions kept,
'erection of' rescues, long-description veto); final scope sign-off pending.
**Context:** DB is 363 MB (Supabase free-tier threshold 500 MB). The pagination
fix ([2026-10-13-haringey-ingestion-gap-investigation.md](2026-10-13-haringey-ingestion-gap-investigation.md))
means a full re-backfill of all 33 boroughs — impossible to absorb without
filtering. Goal: keep substantial architectural projects (new builds,
conversions, **extensions** — a favourite lead source per owner), purge or
never register pure minor works (adverts, telecoms, window swaps, solar…).

## Data baseline (2026-10-14)

| Table | Size | Rows |
|---|---|---|
| applications | 185 MB | 147,923 |
| ui_project_root_tsv | 56 MB | 112,848 |
| projects | 42 MB | 112,848 |
| project_applications | 39 MB | 147,908 |
| grouping_review | 19 MB | 7,758 |
| **total DB** | **363 MB** | |

Root app_type mix: Full 66.9k, Outline 21.3k, Heritage 6.5k, Conditions 5.6k,
Advertising 5.0k, Amendment 3.9k, Telecoms 0.7k. 94k Small / 9.2k Medium /
2.1k Large.

## Project anatomy (agreed vocabulary)

- **root** — the very first application ever submitted for the scheme.
- **main** — the application carrying the most accurate description of the
  scheme. In the current schema: the earliest member with `role='original'`
  (= `projects.root_uid` in `clean` projects).
- **children** — follow-ups: `variation` (s73), `nma` (s96a), `discharge`
  (conditions) per `pam.projects.role_of`.

Membership of the 112,848 projects: 103,032 clean (main present), 1,761
provisional/ambiguous WITH an original member (main present; older ancestors
unresolved — classifiable), **8,055 provisional/ambiguous with NO original
member — main never fetched** (of which only 928 still have a pending
`missing_parents` row; the rest are exhausted fragments).

## The classifier — pam/classify.py (final ruleset, owner-tuned)

Two-stage, conservative. A record/project is **minor** only when BOTH:

1. a minor-work bucket matches the description (windows/doors replacement,
   loft/dormer phrasing, solar PV, telecoms, adverts/signage, fences/walls/
   gates, drop kerbs, HVAC/plant, bins/cycle stores, EV chargers,
   scaffolding/temporary, internal-only works, basement-only, roof repairs,
   garden trivia — sheds/decking/pergolas/carports), or app_type ∈
   {Advertising, Telecoms, Trees}; AND
2. **no keep signal fires**: app_size Medium/Large; n_dwellings ≥ 2;
   **'erection of' / 'construction of' anything** — unless the erected object
   within ~35 chars is itself a minor structure (fence, gate, wall, sign,
   mast, antenna, shed, hoarding, bin/cycle store, substation, kiosk,
   bollard, railing, scaffold, palisade, decking, pergola, carport;
   corpus-verified: 'erection of' objects are overwhelmingly real building
   work, fences/signs are usually 'installed'); **the word 'extension'**
   (extensions are prime leads, so extension projects mentioning rooflights/
   windows/solar ancillaries are never purged for those mentions); N
   flats/dwellings/units/houses/properties counts; conversion to
   flats/dwellings/properties/HMO; change of use to residential; demolition+
   rebuild; redevelopment / mixed-use; major refurbishment; and a
   **long-description veto** at ≥ 750 chars (multi-part schemes; does not
   apply when every hit is pure utility content — telecoms/adverts/satellite/
   scaffold).

Categorical app_types (Advertising/Telecoms/Trees) purge outright — NO keep
rescue, per owner: "erection rescues anything EXCEPT the clearly excluded
PlanIt groups". ('build' was considered and rejected on data: only 722 hits,
dominated by 'build-to-rent'/'build contract award' noise — 'erection'/
'construction' are the construction verbs of this corpus, 39.5k + 13k hits.)

Validated: 22-case assertion suite (all pass) +
scripts/analyze_project_descriptions.py eyeball harness (random samples of
every set reviewed: 30/30 purge samples genuinely minor).

### Corpus results (classifying by MAIN description, final ruleset v11)

| Set | Projects | Applications |
|---|---|---|
| PURGE | 20,932 (18.5%) | 23,568 |
| GREY (minor phrase + keep signal) | 28,386 | — |
| KEEP (no minor hit) | 55,493 | — |
| POSTPONE (main not in DB) | 8,036 | 12,875 |

Purge-set composition (multi-label): windows_doors 5.5k, Advertising 4.9k,
adverts_signage 4.2k, fence/wall/gate 2.4k, HVAC 2.3k, rooflight-only 1.1k,
kerbs ~0.9k, basement ~0.9k, satellite ~0.8k, Telecoms 0.7k, bins 0.7k,
garden_minor 0.7k, telecoms-desc 0.6k, internal-only 0.6k, solar 0.6k,
roof works 0.6k, EV 0.3k, scaffold 0.2k.

- Purge-set mains citing a parent ref: **1** (main-application rule works).
- Lead rows inside purge projects: **14,118 of 91,751 (15.4%)**.
- Record-level ingest simulation: 22,857 / 147,948 (**15.4%**) blocked;
  28,951 parent-citers (19.6%) exempt at ingest.
- Size projection: purge frees ~55 MB, tombstones ~5 MB ⇒ **net ~51 MB**
  (363 MB → ~312 MB). Headroom ~190 MB; retention remains the size governor.

## Purge algorithm (existing DB)

Per project, in batches:
1. Protected? (`ui_project_stars` / `ui_project_meta` rows, same rule as
   retention) → keep.
2. Main (earliest `original`) present? → classify by its description.
   Not present → **postpone** (no-op; revisited after parent hunts or by a
   periodic sweep).
3. Minor → purge the whole project:
   - write tombstones (below) for every member reference + address key;
   - delete member applications (keeping any also claimed by a surviving
     project — the retention dual-membership guard), project_applications
     (cascade), grouping_review (cascade), missing_parents followers,
     ui_project_root_tsv / ui_project_agent_presence rows (explicit),
     orphaned state_history.

## Tombstones (no re-fetch registry)

New worker-owned tables:

```sql
dismissed_projects (id, authority, address_key, main_reference,
                    reason, n_members, dismissed_at)
dismissed_refs     (authority, reference, dismissed_project_id,
                    PRIMARY KEY (authority, reference))
```

Why both: children cite the **root**, or **another child**, or **nothing**
(grouping by address). So a dismissal must record *every member's reference*
(catches citation-bearing children) AND the `(authority, address_key)`
(catches citation-free address-grouped children). ~15 MB for the full purge
set — cheap insurance against re-ingesting 80k records one by one.

## Ingest decision flow (pam.main.execute, per record)

1. **Keep signal** on own description (or Medium/Large / n_dwellings≥2)
   → always store. (A new major at a previously dismissed address re-opens
   the address: the old project's rows stay purged; a fresh project forms.
   projects.UNIQUE(authority, address_key) is unaffected because tombstones
   live in a separate table.)
2. Cites a ref ∈ `dismissed_refs` → skip.
3. No citations AND `(authority, address_key)` ∈ `dismissed_projects` → skip.
4. Own description minor AND role is original (no parent citation) →
   skip + write tombstone (its reference + address key) so its future
   children are skipped too.
5. Otherwise → store (conservative; includes all non-original follow-ups
   citing live/unknown parents).

Both dismissed tables are small enough to cache in memory per run.

## Project-level verdict at grouping time

`projects.group()` re-derives the root/main whenever members change. After
root assignment, classify the main's description: minor ⇒ purge the whole
project immediately (+ tombstones). This closes the loop for newly arriving
mains of previously fragment-only projects, without waiting for a sweep.

## Open policy questions

1. ~~Loft conversions~~ RESOLVED (owner, 2026-10-14): keep loft conversions /
   dormers; only rooflight/skylight-ONLY jobs purge (bucket
   `rooflights_only`, ~1.1k projects).
2. Heritage minor works (2,454 of the purge set): listed-building small works
   (window swaps on listed houses etc.) — purge with the rest, or exempt
   app_type=Heritage?
3. Postpone set (8,036): agreed — never auto-purged; revisited after parent
   hunts or by a periodic sweep. Exhausted fragments stay indefinitely unless
   the owner decides otherwise.
4. Word-number multi-dwellings ("subdivision into two dwellings") purge
   today (digit-based counts only). Add number-words to the keep side?

## Implementation checklist (after sign-off)

- [x] pam/classify.py (validated by the analysis harness)
- [ ] fetch fix: deterministic sort + server-driven paging + reconcile
      (pam/api.py, pam/main.py stats, fetch_progress meta)
- [ ] `worker enqueue-backfill --chunk month`
- [ ] dismissed_projects / dismissed_refs tables + ingest flow
- [ ] scripts/purge_minor_projects.py (batched, protection-aware)
- [ ] grouping-time project verdict hook
- [ ] sequencing: deploy ingest filter → purge → monthly re-backfill all
      boroughs → resume normal syncs
