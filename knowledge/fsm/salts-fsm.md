# Salts FSM - how our field service management app works

Summary: what Salts FSM is, its main records and workflows, and how Jarvis connects to it. Extend this as the
app evolves - Jarvis reads it on every conversation, and can also read the source code on GitHub.

## What it is
Salts FSM is the company's own web application for running jobs, engineers, customers, sites, maintained
systems, contracts and quotes. It is hosted on Azure App Service (UK South), with a separate test deployment.
Customers can accept quotes through a public quote link (`?public=1#quote=...`).

## Main records
| Record | What it holds | Notes |
|---|---|---|
| Customer | Company/organisation we invoice | One customer can have many sites |
| Site | A physical address we work at | Holds access notes, contacts, systems |
| System | A maintained system on a site | Type (fire alarm, EL, intruder, CCTV, access control, extinguishers), make/model, service frequency, last and next service date |
| Contract | Maintenance agreement | Renewed as a whole; all systems on a contract share one renewal date |
| Job | A visit or piece of work | Type (service, call-out, remedial, install, commissioning, survey), status, engineer, scheduled/started/completed times, value |
| Quote | Priced proposal | Status draft/sent/accepted/declined; public acceptance link |
| Engineer / staff | People and their qualifications | Certification names and expiry dates |
| Timesheet | Hours worked per engineer per day | Used for utilisation |

## Key workflows
1. **Planned maintenance:** systems due a service appear on the schedule; the office books a job; the engineer
   completes a job sheet on their phone (with photos) and the service certificate is issued; remedials found
   become quotes.
2. **Call-outs:** a fault is reported, a job is created with an SLA priority, an engineer attends, and the
   job is completed and invoiced.
3. **Quotes:** a quote is prepared, sent with the public link, accepted online, then turned into an install job.
4. **Contract renewals:** contracts coming up for renewal are reviewed, uplifted and renewed.

## How Jarvis connects
- **Read:** REST API under `FSM_BASE_URL` + `FSM_API_PREFIX`, with the routes in `fsm_endpoints.yaml` (jobs,
  engineers, systems, contracts, quotes, sites, customers, timesheets). Jarvis can also GET any other API path.
- **Field names are normalised, not assumed.** `jarvis/integrations/fsm.py`'s `ALIASES` maps each record's real
  field onto every spelling Salts FSM's API (or a future version of it) might use for it - a job's reference
  might come back as `ref`, `reference`, `jobNumber`, `job_number` or `number` depending on the endpoint or a
  later API change, and Jarvis reads any of them as the same `ref` field. This is why a bare `fsm_query` (a
  raw GET) can show field names that look different from what the other FSM tools report - both are correct,
  one is normalised and one isn't. If Salts FSM's API adds a genuinely new field with no equivalent yet, add
  it to `ALIASES` rather than reading it ad hoc each time.
- **Write:** only through `fsm_change`, which queues a change for Alex's approval.
- **Source code:** the GitHub repository in `FSM_REPO` - Jarvis can search and read it to explain features,
  and its engineering agent prepares bug fixes as pull requests.
- **Routine tests:** HTTP smoke checks in `routine_checks.yaml` run every 15 minutes against the live site.
- **Deploys:** merged fixes deploy to Azure through the repo's GitHub Actions workflow (or Kudu zip deploy),
  then the smoke tests re-run.

## Already handled inside Salts FSM
- **Remedial quotes:** defects found on service visits are raised as remedial quotes in Salts FSM. Jarvis does
  not create them - it watches the remedial pipeline and chases quotes that stall (`remedial_quotes`).

## Known quirks / FAQs
- (Add notes here as you learn them, e.g. "photo uploads fail on slow mobile data - retry on Wi-Fi".)
