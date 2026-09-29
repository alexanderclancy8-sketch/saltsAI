---
title: Maintenance contracts, scheduling, SLAs, KPIs and paperwork
aliases: [PPM scheduling, maintenance contracts, KPIs reference, SLAs]
tags: [operations, ppm]
type: note
summary: Contract types and renewals, visit frequencies, PPM vs reactive work, SLAs, remedial quotes, KPI formulas, paperwork, and lawful staff/vehicle monitoring.
created: 2026-09-28
updated: 2026-09-29
status: growing
related: ["[[uk-tax-and-accounting]]", "[[certification-and-competency]]", "[[standards-moc]]", "[[operations-moc]]"]
---

# Maintenance contracts, scheduling, SLAs, KPIs and paperwork for a fire and security service business

Summary: How a UK fire and security maintenance business typically runs. Covers contract types and renewals, visit frequencies by system, planned (PPM) vs reactive work, SLAs and out-of-hours, remedial quotes and defect categories, KPIs with formulas, paperwork, and lawful staff and vehicle monitoring under UK GDPR.

Status: Written September 2026 as general industry good practice. It is not specific to any customer. Frequencies come from the relevant standards (see the `standards/` files). Anything marked **(verify)** should be checked against the standard or the contract. Target KPI values are indicative only.

## Quick reference: visit frequencies by system

| System | Standard | Contractor visit frequency (typical minimum) | User / site checks |
|---|---|---|---|
| Fire alarm (non-domestic) | BS 5839-1 | **Every 6 months** (5–7 month window). Quarterly on large sites | Weekly MCP test, logbook |
| Fire alarm (domestic Grade A/C) | BS 5839-6 | Per standard and manufacturer, commonly 6–12 months **(verify)** | Regular user test |
| Emergency lighting | BS 5266-1 / BS EN 50172 | **Annual** full-duration test. Many contracts include 6-monthly or monthly visits | **Monthly** flick test |
| Intruder alarm (monitored) | BS 9263 / PD 6662 | **2 visits a year** (one may be remote under conditions) | User set/unset, report faults |
| Intruder alarm (bells-only) | BS 9263 | **1 visit a year** | As above |
| CCTV | BS EN 62676-4 | Annual. Twice a year for BS 8418 monitored systems **(verify)** | Check recording, cleaning |
| Access control | BS EN 60839-11 | Annual, or 6-monthly for critical sites | Report door faults |
| Door holders / free-swing closers | BS 7273-4 | With each fire alarm service | Weekly release with the alarm test |
| Aspirating detection | BS EN 54-20 / BS 5839-1 | With fire alarm (≤ 6 months) | Panel status |
| Voice alarm | BS 5839-8 | With fire alarm (≤ 6 months) | Weekly test with the fire alarm **(verify)** |
| EVC / refuge systems | BS 5839-9 | Commonly 6-monthly **(verify)** | Periodic test |
| Portable extinguishers | BS 5306-3 | **Annual** basic service. Extended service at 5 years. CO2 overhaul at 10 years | Monthly visual check |
| Gas / kitchen suppression | BS EN 15004 / BS EN 17446 | 6-monthly or annual per standard, manufacturer and insurer **(verify)** | Check status indicators |
| Dry risers | BS 9990 | 6-monthly visual, annual pressure test **(verify)** | — |

## Contract types

| Type | What's included | Pros / cons |
|---|---|---|
| **PPM only** (planned preventive maintenance) | Scheduled visits at a fixed annual price. Call-outs, parts and remedials are charged separately | Simple and low price, so it wins tenders. Reactive work is billable. Customers can be surprised by extra charges |
| **PPM + reactive at agreed rates** | As above, plus agreed call-out rates, labour rates and parts margins | Most common. Transparent |
| **Comprehensive / fully inclusive** | Visits plus call-outs and labour (sometimes parts). Excludes consumables, batteries, damage, misuse and upgrades | Predictable for the customer. Salts takes the risk, so price it with realistic call-out history and system age |
| **Monitoring bundle** | Maintenance plus ARC monitoring and signalling path fees | Recurring revenue. The ARC contract sits behind it |
| **Framework / FM subcontract** | Work via an FM company or main contractor, on their SLAs and paperwork | Volume, but tighter margins and payment terms. CIS/reverse-charge may apply to some work (see `finance/uk-tax-and-accounting.md`) |

**Key contract terms to check or include:**
- System description and quantities (panel, devices, luminaires, extinguishers, cameras, doors). Price changes when quantities change.
- Visit frequency and scope (what is tested on each visit), and the standard referenced.
- SLAs for reactive work, and out-of-hours availability and rates.
- Exclusions: batteries, consumables, damage, false alarm investigations, and remedials to pre-existing non-compliance.
- Access: site hours, permits, asbestos information, parking, escorting.
- Term, auto-renewal, notice period and annual price review (for example CPI-linked).
- Payment terms (30 days typical), invoicing frequency (annually or quarterly in advance), late payment interest.
- Liability caps and insurance.
- Customer responsibilities: weekly tests, logbook, notifying changes, ARC contacts, access.
- **Domestic customers:** consumer law applies. There are cancellation rights for off-premises/distance contracts (14 days), and terms must be fair and transparent.

**Takeover of a system from another contractor:**
- Price a **special/takeover inspection** (BS 5839-1 recommends one).
- Get engineer codes and configuration from the customer. Check the logbook and certificates, and note existing non-compliances in writing at the start.

## Contract register and renewals

Each contract record should hold:
- the site (name only, internal system)
- systems covered and quantities
- visit frequency
- **last visit date and next due date**
- contract start date, **renewal date** and notice period
- annual value
- SLA level
- ARC and signalling details
- access notes
- any special requirements (asbestos, permits, security clearance)

**Renewal workflow:**
- Review renewals 90 days before the renewal date.
- Issue renewal notices and price increases 60 days before (or per the contract).
- Chase at 30 days, then confirm renewal or record the loss reason.
- **Lost contracts:** record the reason (price, service, site closed, taken in-house by FM). Tell the customer in writing that maintenance will lapse. For police URN intruder systems and monitored fire alarms, remind them of the consequences (URN, insurance, legal duty to maintain).

## Planning and scheduling PPM

- **Due-date logic:** next due = last completed visit + frequency. For fire alarms, keep every visit within 6 months of the last one (flag at 5 months, book by 5.5 months, **escalate at 6 months**; beyond 7 months is non-compliant).
- Book visits 2–4 weeks ahead. Confirm access, the site contact, permits and ARC notification the day before.
- **Bundle systems per site.** Do fire alarm, emergency lighting and extinguishers in one trip where the frequencies align. Consider moving anniversaries to line them up at renewal.
- **Cluster geographically** (for example West Yorkshire areas) to cut travel time. Plan routes daily.
- Match engineer skills and manufacturer training to the systems (Gent vs Apollo-protocol panels, intruder panels, CCTV/IP).
- Plan for **seasonal constraints**:
  - schools want works in holidays
  - retail avoids peak trading
  - care homes want quiet periods
  - annual EL duration tests need out-of-hours or alternate-fitting arrangements
- **Van stock:**
  - common detectors, bases, MCP glasses and elements
  - batteries (common sizes)
  - EL batteries and fittings
  - extinguisher spares and seals
  - test equipment
  Restock from job usage.
- Leave capacity for reactive work (for example 20–30% of engineer time, based on history).

## Reactive work, SLAs and out-of-hours

| Priority | Example | Typical attendance target |
|---|---|---|
| **P1 Emergency** | Fire alarm can't be reset or silenced, system totally down, no fire cover in sleeping premises, intruder alarm won't set on a monitored site | **4 hours** (24/7) |
| **P2 Urgent** | Partial loss of cover (zone/loop fault), repeated false alarms, ARC path failure | **8 hours** / same working day |
| **P3 Routine** | Single device fault with cover maintained, EL luminaire failed | **24 hours** / next working day |
| **P4 Planned** | Non-urgent remedials, minor defects | Within 5–10 working days, or as quoted |

- BS 5839-1 expects the servicing organisation to provide an emergency call-out service with a defined attendance time. Older editions referred to attendance within 8 hours **(verify the current recommendation)**. NSI/SSAIB security schemes commonly expect 4-hour attendance for monitored systems **(verify)**.
- **Measure three things:** response (acknowledged), attendance (engineer on site) and fix (resolved). Contracts should say which one the SLA refers to.
- **Out-of-hours:**
  - on-call rota with standby payments
  - call handling (in-house or a call-handling service)
  - phone triage before dispatch
  - premium rates
  - a lone working procedure
- **Phone triage script (fire alarm):**
  1. Is there a fire or smell of burning? If yes, evacuate and call 999.
  2. What does the panel display say exactly? (Ask for a photo.)
  3. Is it a fire signal or a fault?
  4. Is it one device or zone, or many?
  5. When did it start?
  6. Any building works, weather or cleaning?
  7. Is the ARC involved?
  - Guide silencing and reset only as far as the site's responsible person is trained. **Never advise disabling a system without advising interim measures** (fire watch, FRA review) and recording the advice.
- Record every call-out: time logged, time attended, cause, fix, parts and whether it was first-time fixed.

## After the service visit: defects and remedial quotes

- Engineers record defects on the job sheet or app with a location, description, photo, priority and suggested fix.
- The office raises a **remedial quote** within an agreed time (for example 2 working days). Follow up at 7 and 14 days.
- **Report life-safety defects in writing to the responsible person the same day.** If the customer declines or delays, record it. This protects Salts and prompts action.

**Defect categories:** there is **no single industry-standard fire alarm coding** like the EICR C1/C2/C3 codes, so many firms use a similar scheme:

| Category | Meaning | Example |
|---|---|---|
| **Cat 1 / Urgent** | Immediate risk to life. System or part not working | Loop down, no sounders in an area, panel dead, EL failed on a stair |
| **Cat 2 / Required** | Reduces effectiveness or compliance and needs fixing soon | Batteries failed load test, missing detector in a new room, obstructed detector |
| **Cat 3 / Advisory** | Improvement, obsolescence or a shortfall against the current standard | Panel obsolete (no spares), recommend multi-sensors to reduce false alarms, upgrade signalling for the PSTN switch-off |

- Carry forward open defects on every certificate until closed.
- Track the **quote conversion rate** and the value of outstanding remedials. It is a major revenue source.

## KPIs (definitions and indicative targets)

| KPI | Formula | Indicative target | Why it matters |
|---|---|---|---|
| **First-time fix rate** | Reactive jobs resolved on the first visit ÷ total reactive jobs | 75–85%+ | Customer satisfaction and margin. Low values suggest van stock or skills gaps |
| **PPM completion %** | PPM visits completed in the period ÷ PPM visits due in the period | 95–100% | Compliance and contract revenue recognition |
| **Overdue visits** | Count (and list) of systems past their due date. Fire alarm past 6 months, or beyond the 7-month outer limit | **Zero** beyond tolerance | Legal and standard compliance. Audit finding. Insurance |
| **Callout response time** | Average (and % within SLA) of time from call logged to engineer on site | ≥ 95% within SLA | Contract compliance |
| **Engineer utilisation** | Chargeable hours ÷ available paid hours | 70–85% | Productivity. Very high values leave no reactive capacity |
| **Revenue per engineer** | Total service and installation revenue ÷ FTE engineers (monthly/annual) | Benchmark internally over time | Overall productivity and pricing |
| **Quote conversion** | Quotes accepted ÷ quotes issued (by number **and** by value) | Track trend; 30–50% on remedials is common **(indicative)** | Sales effectiveness. Chase process |
| **Quote turnaround** | Days from defect found to quote sent | ≤ 2–3 working days | Conversion falls as delay rises |
| **Recurring revenue %** | Contract (maintenance + monitoring) revenue ÷ total revenue | Higher is more resilient | Business value and cash-flow stability |
| **Contract retention / churn** | Contracts renewed ÷ contracts due for renewal | 90%+ | Growth. Losses signal service or price issues |
| **Repeat call rate** | Call-outs to the same fault within 30 days ÷ total call-outs | Low and falling | Fix quality |
| **Certificates issued on time** | Certificates sent within X days of visit ÷ visits | ~100% | Customer compliance evidence |
| **False alarm rate per site** | False alarms ÷ (detectors / 100) per year | Falling trend | Customer value. Remedial opportunity |
| **Job margin** | (Invoice value − labour − materials − subcontract) ÷ invoice value | By job type | Pricing accuracy |

Report monthly. Show trends, not just numbers, and drill down by engineer, customer and contract type.

## Paperwork and records

| Document | When | Notes |
|---|---|---|
| Job sheet / work order | Every visit | Time on and off site, work done, parts used, defects, customer signature, photos |
| BS 5839-1 inspection and servicing certificate | Every fire alarm service | Records devices tested, variations carried forward, defects |
| Commissioning / installation / design / acceptance certificates | New installations and modifications | See `standards/fire-detection-and-alarm-bs5839.md` |
| BAFE certificate of compliance | For each certificated module performed | SP203-1 firms |
| Emergency lighting test certificate | Annual test (plus monthly records if contracted) | Record luminaire failures by location |
| Extinguisher service report and labels | Each service | Asset list with manufacture dates and next extended service/overhaul |
| Intruder alarm maintenance record / certificate | Each visit | NSI/SSAIB format. ARC tests recorded |
| CCTV / access control service report | Each visit | Field-of-view reference images, retention achieved |
| Logbook entries | Every attendance | The customer's logbook on site, not just Salts' system |
| RAMS, permits, asbestos check | Before work | Site-specific. Signed |
| Remedial quote | After a visit | Linked to the defect list |
| Handover pack / O&M | Project completion | Drawings, cause and effect, certificates, manuals, training record (Regulation 38 information) |

- A digital job management (field service) system should store certificates as PDFs against the site. It should track due dates automatically and make reporting easy. Photos help defend disputes and support quotes.

## Lawful staff and vehicle monitoring (UK GDPR)

Monitoring engineers (vehicle trackers, job app GPS, dash cams, phone call recording) is lawful when done properly. The ICO's guidance on monitoring workers (2023) sets the expectations.

**Principles:**
- **Lawful basis:** usually legitimate interests (safety, customer response, security of vehicles and stock, accurate timesheets). Write a short legitimate interests assessment. Consent is usually **not** appropriate for employees because of the power imbalance.
- **Transparency:** tell staff **what** is monitored, **why**, **when**, **how data is used** (including whether it may be used in disciplinary matters), who can see it and how long it's kept. Do this in a monitoring policy and the staff privacy notice, **before** it starts.
- **Proportionality:** use the least intrusive method that achieves the purpose. Avoid monitoring outside working hours or in private moments.
- **DPIA:** do one for vehicle tracking, dash cams (especially inward-facing or with audio), biometric clocking, or anything systematic. It is good practice even when not strictly required.
- **Data minimisation and retention:** keep tracking data only as long as needed (for example a set number of months for timesheet and dispute purposes). Delete it on schedule.
- **Access and security:** limit who can view data and log access. Staff have the right to request their data (subject access).
- **No covert monitoring** except in exceptional cases (a specific suspicion of criminal activity or serious malpractice). It must be authorised at senior level, time-limited and documented.

**Vehicle tracking specifically:**
- Fit **notices in each vehicle** stating that a tracking device is fitted, and explain it in the policy.
- If **private use** of vans is allowed, provide a **privacy mode** or disable tracking outside working hours, unless there is a clear, documented justification (for example stolen vehicle recovery only, with location data not reviewed).
- Use the data for the stated purposes: routing, response, safety, verifying hours, theft. Don't quietly extend it to new purposes without telling staff.
- Telematics driver-behaviour scores: explain how they are used. Coaching first is good practice.

**Other monitoring:**
- **Dash cams:** outward-facing is less intrusive. Audio and inward-facing cameras need strong justification. Switch audio off by default.
- **Job app GPS and timestamps:** same principles. Collect location during jobs and working time only.
- **Biometric clocking** (fingerprint, face): special category data. Needs an Article 9 condition and a DPIA, and **offer an alternative**. The ICO has taken enforcement action against an employer over biometric attendance systems.
- Consult staff before introducing new monitoring and review it periodically (is it still necessary?).

## Related files

- [[standards-moc]] (all system standards)
- [[certification-and-competency]] (auditors, record retention)
- [[uk-tax-and-accounting]] (billing, credit control, CIS/VAT on FM work)
