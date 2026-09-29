# Completion Estimation Framework (draft v1)

**Purpose:** predict when a permitted project will *complete*, so outreach to
the architecture practice lands ~2 months before that date. All numbers are
calibrated guesses — the goal is a defensible rough ordering, not precision.

**Status:** standalone analysis doc, not yet wired into the pipeline.
Grounded in a 2026-09-29 query of `data/ledger.sqlite` (773 leads, Enfield +
Greenwich 3-month backfill).

### Validation against full dataset (2026-09-29, 1,233 leads / 8 boroughs)

The framework **stands**; four refinements from the bigger sample:

1. **C0 discharge lane is bigger and universal: 341 leads (28%), every
   borough** — Southwark alone has 106. (Framework draft said "Enfield-heavy";
   wrong.) But phrasing varies: only 217 contain "pursuant" — 81 say
   "condition" otherwise, 11 "discharge", 32 other. Detection must match all.
2. **New sub-category C0b: "Variation of condition N of reference …"** (~81
   leads) — s73 amendments on live permissions. Mid-design, not yet building.
   Parent-join applies; timing ≈ parent's original estimate shifted by the
   variation delay. Split from pure discharges (C0a).
3. **`agent_company` adds ~nothing here** — 974 rows, 970 co-occurring with
   the agent_name values we already extract (same values, e.g. "Redwoods
   Projects"). The 83 missing-extraction leads are all `agent_name="See
   source"` junk — the filter is correct; the gap is council-side, not ours.
   Still worth extracting `agent_company` as a fallback for OTHER boroughs
   (some publish company but not person), but expect +0–2% in the current 8.
4. **app_size discrimination confirmed useless**: 1,203 Small / 15 Medium /
   13 Large. Keyword order matters more than ever (C5 demolition before C4
   erection — "demolition" appears in 140 leads, many also matching
   "erection of").

---

## 1. What the data actually gives us

| Field | Coverage (773 leads) | Use |
|---|---|---|
| `decided_date` | ~100% | **Anchor date.** Everything measures from here. |
| `app_size` | ~99% (Small 765 / Medium 9 / Large 6) | Weak alone — almost everything is Small. |
| `app_type` | ~99% | Full / **Conditions** / Outline / Amendment / Advertising / Heritage / Telecoms |
| `description` | ~100% | The real classifier — keyword matching. |
| `permission_expires` | 232/773 (council-dependent) | Latest legal start = decided + 5 yrs (Enfield grants 5-year, not 3). Caps the window: completion ≤ expiry + build duration. |
| `other_fields.decision_issued_date` | 763 | Same as decided_date, mostly. |
| `other_fields.application_type` | 773 | Council's own type string — may beat PlanIt's app_type; unexamined. |
| `other_fields.n_documents` | 773 | Proxy for project complexity? Untested hypothesis. |

### Two PlanIt `app_type` traps (verified from descriptions)

- **`app_type='Conditions'` (341 leads, all 8 boroughs — 28% of everything)
  is NOT "permitted with conditions".** Descriptions read "Details submitted
  pursuant to reference 24/00621/FUL: Secure By Design (20) in respect of
  demolition of 5 no. blocks…" or "Variation of condition 2 of approval
  26/00160/HOU to allow changes to the roof…" — these are **discharge-of-
  condition / s73-variation submissions for projects already approved and in
  (or about to start) construction**. Strongest timing signal we have; see §3.
  Phrasings verified 2026-09-29: 217 "pursuant", 81 "condition" (mostly
  Variation), 11 "discharge", 32 other.
- **`app_type='Outline'` (269 leads) is mostly householder trivia** ("Rear
  dormer and front rooflights", "Single storey rear extension 6m deep") —
  not outline planning permissions. Likely PlanIt mapping the council form
  code (e.g. HH = householder) oddly. Classify by description, not app_type.

---

## 2. Project categories and duration model

Build time = **mobilisation lag** (decision → site start: contracts, building
regs, discharge of pre-commencement conditions, contractor booking) +
**construction duration**. Categories keyed on description keywords
(first match wins — order matters), with `app_size` as a multiplier.

| # | Category (detection sketch) | Mobilisation | Build | Completion from decision | Outreach date (completion − 2 mo) |
|---|---|---|---|---|---|
| C0a | **Discharge of conditions** (`app_type='Conditions'` AND description matches `pursuant`/`discharge of`/`details submitted`) | — | — | **already building** | see §3 |
| C0b | **Variation of condition (s73)** (description starts "Variation of condition") | — | — | **mid-design on live permission** | see §3 |
| C1 | Householder small works (`extension` / `dormer` / `loft` / `roof` + `app_size=Small`, not "new dwelling") | 2–4 mo | 3–5 mo | **~6–9 mo** | decision + 4–7 mo |
| C2 | Larger householder (`two-storey`, `basement`, or multiple of the C1 keywords) | 3–5 mo | 6–9 mo | **~10–14 mo** | decision + 8–12 mo |
| C3 | Conversion / change of use (`conversion`, `change of use` + `flat`) | 4–6 mo | 6–10 mo | **~12–16 mo** | decision + 10–14 mo |
| C4 | Small new-build (`erection of` + `dwelling`/`house`, Small) | 4–8 mo | 9–14 mo | **~15–22 mo** | decision + 13–20 mo |
| C5 | Demolition + redevelopment (`demolition` + `erection`) | 6–12 mo | 14–24 mo | **~20–36 mo** | decision + 18–34 mo |
| C6 | Commercial / institutional (`office`, `school`, `hotel`, `care home`, `commercial`) | 6–12 mo | 12–30 mo | **~18–42 mo** | decision + 16–40 mo |
| C7 | Medium/Large `app_size` (any description) | 6–12 mo | 18–36 mo | **~24–48 mo** | decision + 22–46 mo |
| — | Not a lead for timing: `Advertising`, `Telecoms`, `Amendment`, `Heritage` consent alone | — | — | skip or C1 default | — |

Rationale notes (to be replaced by data, see §5):
- C1: householders typically build soon after grant — money and builder are
  lined up; your "short period after planning is granted" instinct matches.
- C4–C7: pre-commencement conditions, building regs, tendering, and finance
  dominate the lag. `permission_expires` (decision + 5 yr) is the sanity cap.
- Ranges are wide by design — the outreach date is the *range start*; better
  slightly early than after the photographer-worthy scaffolding comes down.

**Keyword detection order (draft):** C0a/C0b (app_type + phrasing) → C5 →
C4 → C6 → C3 → C2 → C1 → fallback C1. (Most-specific first: "demolition +
erection" before "erection" — 140 leads mention demolition, most also match
"erection of"; "two-storey" before plain "extension".)

---

## 3. The C0 fast lane: discharges and variations

These are the **highest-value rows in the dataset for outreach timing** —
341 leads (28%), present in all 8 boroughs (Southwark alone: 106). Two
sub-flavours, both with a parseable parent reference:

- **C0a — discharge of conditions** ("Details/submission of details pursuant
  to…", "discharge of Condition 18…", ~228 leads): the project is financed,
  approved, and actively being built. Detail submissions typically happen
  0–6 months before the relevant build phase.
- **C0b — variation of condition / s73** ("Variation of condition 2 of
  approval 26/00160/HOU to allow changes to the roof…", ~81 leads): mid-design
  on a live permission — an engaged client adjusting the scheme. Timing ≈
  the parent's original estimate, shifted by the variation delay. Arguably
  the *earliest* point at which a scheme is both real and still malleable.

Applying to both:

- **Estimate:** completion ≈ discharge/variation date + (parent project's
  remaining build). Rough default for C0a: **+6–12 months** for small works,
  **+12–24 months** for redevelopment schemes ("demolition of 5 no. blocks"
  is not a loft).
- **Parse the parent reference** (`pursuant to reference 24/00621/FUL`,
  `of approval 26/00160/HOU`, `of Ref: TP/03/2194/REN1` — formats vary by
  council) and look the parent up in the ledger → real description, app_size,
  original decided_date → apply §2 from the *parent's* decision, refined by
  "it's already underway" (C0a) or "design still live" (C0b).
- Caveat: the *practice* on the discharge submission is usually the same as
  the parent's — good — but confirm against the parent record.
- Marketing angle differs: at C0a the building exists; the pitch is
  "photograph the nearly-finished work", possibly the most natural fit for
  your service.

## 4. Proposed lead scoring (for later integration)

```
score = category_confidence
      + (has agent_name)            # the whole point
      + (C0/C5/C6/C7 bonus)         # bigger, photogenic projects
      − (Advertising/Telecoms skip)
due_date_estimate = decided_date + mobilisation_low   # outreach opens
due_date_late     = decided_date + completion_high    # outreach closes
```

Spreadsheet columns when integrated: `est_category`, `est_completion_early`,
`est_completion_late`, `est_outreach_from`, plus `parent_reference` for C0.

## 5. How to calibrate (the honest part)

Guesses → estimates, in order of effort:

1. **Manual sample (1 evening):** take 30 ledger rows across categories, open
   `council_url`/`docs_url`, look for "commencement" / completion notices or
   site photos with dates. Tighten the §2 numbers.
2. **Cross-check `application_type` (council's own string)** — may already
   separate householder/full/major better than PlanIt's app_type.
3. **Parent-join for C0 rows:** measure discharge_date − parent decided_date
   distribution in the ledger → real mobilisation lags per borough.
4. **Long-run truth:** the ledger itself. In 12–18 months, watched Undecided
   records + repeat scrapes of the same sites (revised applications,
   further discharges) sketch real timelines. The system is already
   collecting the raw material.

## 6. Known limits

- No completion data exists in PlanIt — all durations are priors until §5.1/§5.4.
- Householder works dominate (~85% Small): the median lead is a rear extension,
  not a portfolio piece. Category weighting should reflect what you *want*
  (C4–C7, C0-redevelopment), not what's most common.
- `app_size` is nearly useless as a discriminator at the small end; keywords
  carry the classification.
