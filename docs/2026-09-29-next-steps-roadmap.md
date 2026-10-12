# Next-Steps Roadmap (Now / Next / Later)

**Date:** 2026-09-29
**Status:** strategic document only — no code changes implied.
**Inputs:** `completion-estimation-framework.md` and the three AI reactions
(`...Reaction_Claude`, `...Reaction_Gemini`, `...Reaction_KIMI`).

## 1. Goals and constraints (agreed in chat)

- **Target work:** both design-led residential and larger schemes
  (commercial, education, housing).
- **Approach:** volume first. Aim for **4–7 new-lead emails a week plus a
  couple of follow-ups**.
- **Manual effort:** about **one day a week** in the first weeks, aiming for
  **half a day a week** later.
- **Data:** backfill **3 years** for everything, and extend fetching to **all
  London boroughs**, not just the first 8. The first 8 returned the best data
  so far, but a more thorough fetch may turn up other valuable boroughs.
- **Boroughs:** no preference. All of London is within reach.

## 2. What the AI reactions agree on

- The idea is sound. The output is a ranked shortlist with rough windows, not
  precise dates.
- The framework holds: the `Conditions` trap, the C0 discharge/variation
  lane, description keywords over `app_size`, and wide ranges with outreach
  at the start of the range.
- **A 3-month backfill is the structural weakness.** Projects finishing
  soon were permitted 1–3 years ago.
- `agent_name` is often a planning consultant or a volume builder, not the
  architect. Agents need classifying.
- Score for photographability, not just timing.
- Calibrate the duration guesses against about 20–30 real projects.

Distinct additions:

- **Claude:** score discharges by which condition is being discharged.
  Group leads by practice, so each practice gets one email. Other completion
  signals: street naming notices, building control, RIBA lists, past clients.
- **KIMI:** variations (C0b) are the warmest segment. Split non-material
  amendments from s73 variations. Build a "project spine" by address. Gate
  bad dates (1989/1992). Only apply the `permission_expires` cap to real
  permissions, not certificates. Forecast decisions for Undecided rows.
- **Gemini:** photogenic and low-value keyword scoring. Assume 20–30% of
  permissions are never built. Time outreach around RIBA award deadlines and
  editorial calendars. Run a 20-lead pilot.

## 3. Roadmap

### NOW (first weeks): data breadth and first manual loop

1. **Backfill 3 years, all application types.** Householder included, since
   volume comes first. Re-check storage size and scrape time.
2. **Fetch all London boroughs** (32 boroughs plus the City).
   - Use the borough audit (`2026.09.27-borough-audit.csv`) to sequence them.
   - Record per-borough data quality: agent names, descriptions, expiry
     dates, document links.
   - Rank boroughs by number of usable leads. Don't drop any before seeing
     the data.
3. **Data-quality gates:** drop decided dates before the backfill start
   (e.g. the 1989/1992 rows), and apply the expiry cap only to real
   permissions.
4. **Manual calibration, 20–30 leads** across categories. Look for
   commencement notices, site photos and completion dates. Tighten the §2
   duration table of the estimation framework.
5. **Start weekly outreach immediately, without waiting for the tooling.**
   Each week, pick 4–7 leads from the ranked sheet, look up the practice and
   the right contact, and send. Add a couple of follow-ups to previous
   emails. This is the first pilot, and the responses are the real
   calibration data.
6. **Log every outreach:** lead, practice, date sent, reply, outcome. This
   is the ground truth for everything later.

### NEXT (weeks 3–8): making each manual hour count

1. **Parent-join for C0 rows.** Link discharges and variations to the
   parent application, to get the real scope, original decision date and
   likely architect.
2. **Split C0 into lanes:** discharges (C0a), s73 variations (C0b) and
   non-material amendments. Score discharges by condition type: landscaping,
   materials and cycle stores suggest the late stage.
3. **Classify agents:** architect / planner / builder / unknown. Cap volume
   shops such as Redwoods Projects. Where the agent is a planner, find the
   architect through the parent record or the Design & Access Statement.
4. **Photographability score.** Use keywords (listed, conservation area,
   facade, glazing, courtyard, etc.), unit counts, `n_documents`, and the
   agent tier. Include both design-led residential and larger schemes.
5. **Project spine by address.** Merge repeated records for one site. The
   latest discharge marks the current phase. This prevents duplicate
   emails.
6. **Group by practice.** One email per practice covering all its live
   schemes. Rank practices by number of live schemes.
7. **Reduce manual time** by making the sheet do the pre-filtering: agent
   tier, score, and outreach window in the first columns.

### LATER (months 2+): refining and scaling

1. **Replace guessed durations with measured ones.** Use the discharge date
   minus parent decision date, per borough, and the outreach outcomes.
2. **Forecast Undecided rows** (median decision time about 50 days), so
   leads enter the pipeline continuously.
3. **Other completion signals:** street naming notices, building control
   data, RIBA award and longlist data, and architects' own "recently
   completed" posts.
4. **Past clients tab:** watch live projects of practices you already work
   with.
5. **Outreach timing hooks:** RIBA award deadlines and editorial calendars.
6. **Drop-off allowance:** treat outreach for ordinary approvals as a soft
   check-in, since some permissions are never built.
7. **Revisit the borough set.** Drop or deprioritise boroughs that give poor
   data, and adjust the weekly time budget.

## 4. How we will know it works

- 4–7 new-lead emails per week are achievable without more than a day of
  manual effort.
- Reply and meeting rates per lead type (C0a, C0b, residential, larger) show
  which lanes are worth the time.
- Manual time per week trends toward half a day.

## 5. Open questions and decisions

- Storage and scrape time for a 3-year, all-London backfill.
- Which boroughs publish agent or architect data usefully.
- How to find contacts efficiently (studio websites, LinkedIn, planning
  documents).
- Whether follow-ups should follow a fixed cadence.


## 6. Parent resolution + retention (planned, 2026-10-04)

Context: project grouping is live (`projects` / `project_applications`).
Many `parent_ref`s do not resolve because the parent predates the 3-year
backfill. Verified: PlanIt has **no** by-uid/by-reference lookup (the
undocumented `search` param matches description text only), so parents must
be fetched via targeted mini-backfills.

- **One-off parent hunt:** collect unresolved `parent_ref`s, parse the year
  embedded in each ref (`P2018/…`, `2018/1234/P`, `PA/18/…`), and enqueue a
  `start_date`-window backfill job per (authority, year). Est. 30–60 windows,
  a few hours of polite API time. Rerun grouping afterwards.
- **Daily:** each sync job collects unresolved refs from newly fetched rows
  and enqueues the same mini-backfills (usually zero or one window per day).
- **Retention (separate job, nightly):** delete `applications` rows where
  `last_updated < today - 3 years` AND their project has had no activity in
  3 years. Keep `state_history` regardless. Guard: never delete members of
  recently-active projects — grouping gives correctness, retention only buys
  space.
