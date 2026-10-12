# Council-by-Council APIs — Research Findings

**Date:** 2026-09-27
**Question:** do individual London boroughs (Islington, Camden, Westminster, Hackney, Haringey) offer public APIs or open data feeds for planning applications — and would a council-by-council approach beat PlanIt?

## Verdict (short)

**Council-by-council is not viable as a primary source.** Only 1 of 5 target boroughs (Camden) publishes application-level open data at all — and even that feed omits agent names. Four boroughs would require fragile bespoke scrapers of legacy planning portals (Northgate, Idox, Arcus). Keep PlanIt as the backbone; optionally enrich Camden leads via its Socrata feed.

## Per-council findings

### Camden ✅ — the one good feed

- **Dataset:** "Planning Applications" on Socrata open data portal, ID `2eiu-s2cw`, records back to 2010
- **API:** full Socrata SODA API — `https://opendata.camden.gov.uk/resource/2eiu-s2cw.json` with SoQL filtering (`$where`, `$limit`, date ranges), plus CSV export
- **Back-office:** Northgate; portal links point to `planningrecords.camden.gov.uk`
- **Fields (33 columns):** `application_number`, `development_address`, `development_description`, **`applicant_name`** ✅, **`decision_type`** ✅ (Granted/Refused/Withdrawn), `decision_date`, `decison_level` (Delegated/Committee), `system_status`, `case_officer`, `case_officer_team`, `ward`, `conservation_areas`, `application_type`, lat/lng + easting/northing, `full_application` URL
- **No `agent_name`** — notable gap even here
- **Licence:** OGL v3 (+ OS attribution; points snapped to nearest postcode)
- **Update frequency:** daily (verified: last upload 2026-09-26)

### Islington ❌

- No planning-applications open dataset anywhere (council site, London Datastore, data.gov.uk)
- Portal only: **Northgate Planning Explorer** (legacy ASP.NET, session-based, painful to scrape)
- Agent/applicant shown per-record in portal UI only
- This is the council where PlanIt's `agent_name = "See source"` junk already hurts — and there's no clean alternative

### Westminster ❌

- Publishes only Article 4 / Conservation Area / Listed Building / TPO CSVs (OGL) on its Digital Planning Data page — **no applications dataset**
- Portal: **Idox Uniform** (idoxpa.westminster.gov.uk) — UI only

### Hackney ❌

- Portal: **Idox Cloud**; ArcGIS "check local planning information" map exists
- No applications open dataset

### Haringey ❌

- Portal: **Arcus (Salesforce-based)** public register
- Open planning datasets page has Article 4 / conservation / listed CSVs only — no applications dataset

### Pan-London: GLA Planning London Datahub ⚠️

- Live platform at planninglondondatahub.london.gov.uk aggregating applications from **all London LPAs**, with map UI, query builder, CSV export
- **No documented public API** — JS frontend over an internal service; unofficial endpoints may exist but are undocumented/unsupported
- Data is LPA-supplied and explicitly not quality-checked; agent/applicant names unlikely in the public schema (privacy-conscious design)
- Worth a future DevTools investigation; not a dependable pipeline today

## Comparison

| Capability | PlanIt | Council-by-council |
|---|---|---|
| Uniform coverage of 5 boroughs | ✅ | ❌ (1/5 has a feed) |
| Agent name | ❌ | ❌ (even Camden omits it) |
| Applicant name | ❌ | ✅ Camden only |
| Decision outcome | ✅ | ✅ Camden only |
| Engineering effort | Done (client designed) | 4 fragile bespoke scrapers + 1 Socrata client |

## Consequences for the build

1. **PlanIt remains the backbone** — decision confirmed for the second time, from a different angle.
2. **Camden Socrata enrichment (optional v1.5):** for leads in Camden, join on application reference to pick up `applicant_name`, `decision_type`, `case_officer`, `conservation_areas` — daily, reliable, OGL-licensed, trivial SoQL client. The modular `api.py` interface seam (normalized records) accommodates this cleanly.
3. **Agent names stay v2:** the only route for Islington/Westminster/Hackney/Haringey is per-record portal scraping — and it should only ever run against shortlisted leads (via the `url`/`docs_url` PlanIt provides), not bulk.

---

*Companion docs: [2026.09.27-planit-project-brief.md](2026.09.27-planit-project-brief.md), [2026.09.27-planit-api-findings.md](2026.09.27-planit-api-findings.md), [2026.09.27-planning-data-gov-uk-comparison.md](2026.09.27-planning-data-gov-uk-comparison.md).*
