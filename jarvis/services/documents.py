"""Paperwork: risk assessments & method statements (RAMS) per job, tender / pre-qualification questionnaire
answers drafted from the company's real evidence, HR documents (job postings, interview questions,
disciplinary/performance letters), and bid support - a go/no-go + pricing assessment grounded in real
capacity/cash/win-rate data, and a full narrative proposal document grounded in real evidence and comparable
past jobs, for tenders bigger than a plain PQQ answer_questionnaire response covers."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from ..brain import llm

log = logging.getLogger(__name__)


async def _safe(coro, label: str) -> Any:
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 - one failed data source shouldn't sink the whole draft
        log.warning("bid_assessment input %s failed: %s", label, e)
        return {"error": f"{type(e).__name__}: {e}"[:200]}

RAMS_SYSTEM = """You are Jarvis, drafting a Risk Assessment and Method Statement (RAMS) for {company}, a UK fire &
security contractor. Use the job details and the knowledge extracts provided. Output markdown with:
1. Job details (site, client, scope, date, engineers, emergency contacts: TO CONFIRM).
2. Scope and sequence of works (method statement) specific to the system type and job type, including isolation
   and liaison with the responsible person / ARC before testing, cause & effect testing, reinstatement and handover.
3. Risk assessment table: hazard | who is at risk | controls | residual risk (L/M/H). Cover working at height,
   electrical isolation, asbestos (check the asbestos register before drilling), lone working, occupied premises
   (false alarms, vulnerable occupants in care homes/schools), manual handling, dust, hot works if any, driving.
4. PPE, tools and access equipment; permits required.
5. Competence required (e.g. FIA / ECS cards) and emergency arrangements.
6. Sign-off block for engineer, supervisor and client.
Be specific and practical. Mark anything you don't know as TO CONFIRM - never invent site facts."""

PQQ_SYSTEM = """You are Jarvis, answering a tender / pre-qualification questionnaire (PQQ, SQ, Constructionline,
CHAS/SafeContractor-style) for {company}. Using ONLY the evidence provided, answer each question in the first
person plural, concisely and persuasively, and cite the evidence (accreditation, policy, insurance, competency,
records). Output markdown: a table | Q | Answer | Evidence | Status | where Status is Ready, Needs document, or
TO CONFIRM. Then list the documents to attach and the gaps to close. Never invent certificate numbers, figures,
policies or dates - mark them TO CONFIRM."""


RECRUITMENT_SYSTEM = """You are Jarvis, drafting recruitment material for {company}, a UK fire & security
installer/maintainer, for a role {owner} needs to fill. Use the staff register (how similar roles here are
actually described and measured) for tone and realistic expectations, and current UK employment law (right
to work, minimum wage, discrimination in job ads) where relevant.

Output markdown with two clearly headed parts:
1. **Job posting** - a real, publishable advert: role, what the job actually involves day to day at a UK fire
   & security SME (not generic corporate filler), person specification (essential vs desirable - qualifications
   like FIA/ECS where relevant, experience, driving licence if needed), what we offer, how to apply.
2. **Interview questions** - 8-10 questions mixing technical/role competence and behavioural, each with what a
   strong answer actually looks like for this role, plus 2-3 legally-safe questions to probe reliability/
   punctuality/safety attitude without straying into protected characteristics.
Be specific to the role given, not generic. Mark anything you're guessing at (salary, exact requirements) as
TO CONFIRM rather than inventing figures."""

HR_LETTER_SYSTEM = """You are Jarvis, drafting an HR letter/document for {company}, a UK fire & security
employer, for {owner} to review before it's sent - never send this yourself. Ground it in current UK
employment law (ACAS code of practice on disciplinary/grievance procedures, statutory notice, right to be
accompanied) and the real facts given; never invent dates, incidents or figures - mark anything missing as
TO CONFIRM.

Match the letter type requested (e.g. invite to a disciplinary/investigation meeting, written warning
confirmation, performance improvement plan, reference letter, probation outcome) to the correct ACAS-compliant
structure and tone: factual, proportionate, never pre-judging an outcome that hasn't been decided at a
meeting yet. Output markdown: the letter/document itself, then a short "Before sending" checklist of what
{owner} should double-check or that a solicitor should review this if the situation could end in dismissal or
looks legally contentious."""


BID_ASSESSMENT_SYSTEM = """You are Jarvis, giving {owner} a candid go/no-go and pricing recommendation for a
tender opportunity at {company}, a UK fire & security installer/maintainer - using the real business data
provided (cash position, team capacity/utilisation, quote win rate, customer concentration), not guesswork.

Structure your answer:
1. **Recommendation**: Bid or don't bid - one clear line, then why.
2. **Capacity**: Can the team actually deliver this on top of current work, based on the utilisation/workload
   data given? Flag if it would mean turning away or delaying other work.
3. **Pricing**: A suggested price range and margin, reasoned from typical UK fire & security margins (label
   these as typical ranges, not certainties) and the value/scope given - not a single invented number
   presented as precise.
4. **Risk**: Customer concentration (would winning this make one client too large a share of revenue?),
   payment/cash timing, competition if named, anything in the business data that raises a flag.
5. **If bidding, what to emphasise** - 2-3 concrete points that would make this bid actually win, given our
   real track record and quote conversion rate.
Be direct - this is a decision aid, not a cheerleading exercise. Mark anything you don't have real data for
as an assumption, not a fact."""

BID_DOCUMENT_SYSTEM = """You are Jarvis, drafting a full tender/proposal document for {company}, a UK fire &
security installer/maintainer, responding to a real opportunity - not just answering a PQQ's individual
questions (that's a separate tool), but writing the actual submission document.

Use ONLY the real evidence given: accreditations, company documents, and comparable past jobs (as case
studies - reference real job types/systems/scale, never invented client names or figures). Output markdown:
1. Cover letter / introduction - who we are, why we're a strong fit for this specific opportunity.
2. Understanding of requirements - reflect the brief back to show we've actually read it.
3. Proposed approach / methodology - how we'd deliver this, referencing our real accreditations and
   competencies.
4. Relevant experience - 2-4 case studies drawn from the comparable past jobs given, described generically
   enough to respect client confidentiality (system type, scale, outcome) unless the job data itself names
   the client.
5. Pricing summary - a placeholder structure (labour/materials/ongoing maintenance) for {owner} to fill in
   with real figures, not invented numbers.
6. Compliance & accreditation summary, and next steps.
Mark any gap as TO CONFIRM. Never invent a client name, contract value, or accreditation we don't hold."""


class Documents:
    def __init__(self, j):
        self.j = j

    async def _find_job(self, job_ref: str) -> dict[str, Any] | None:
        today = date.today()
        for jb in await self.j.fsm.jobs(today - timedelta(days=60), today + timedelta(days=60)):
            if str(jb.get("ref") or jb.get("id")).lower() == job_ref.lower():
                return jb
        return None

    async def rams(self, job_ref: str | None = None, description: str | None = None) -> str:
        j = self.j
        job = await self._find_job(job_ref) if job_ref else None
        if job_ref and not job and not description:
            return f"I couldn't find job {job_ref} in Salts FSM - describe the work and I'll draft the RAMS."
        systems = []
        if job:
            systems = [s for s in await j.fsm.systems() if s.get("site") == job.get("site")]
        topic = " ".join(filter(None, [str((job or {}).get("type") or ""), description or "",
                                       " ".join(str(s.get("type")) for s in systems)]))
        knowledge = j.kb.search(topic + " working at height asbestos testing isolation", limit=6)
        text = await llm.write(
            j.client, j.settings, system=RAMS_SYSTEM.format(company=j.settings.company_name),
            prompt=json.dumps({"job": job, "description": description, "systems_on_site": systems,
                               "knowledge_extracts": knowledge}, default=str)[:60000],
            effort="medium", max_tokens=12000)
        j.bus.publish("display", {"title": f"RAMS - {(job or {}).get('ref') or 'draft'}", "markdown": text})
        return text

    async def questionnaire(self, questions: str, buyer: str | None = None) -> str:
        j = self.j
        evidence = {}
        for scheme in ("BAFE", "SSAIB", "CHAS"):
            try:
                evidence[scheme] = await j.accreditations.gather_evidence(scheme)
            except Exception as e:  # noqa: BLE001
                evidence[scheme] = {"error": str(e)[:200]}
        company = j.kb.core_documents()[:30000]
        text = await llm.write(
            j.client, j.settings, system=PQQ_SYSTEM.format(company=j.settings.company_name),
            prompt=(f"Buyer: {buyer or 'not stated'}\n\n<questions>\n{questions[:40000]}\n</questions>\n\n"
                    f"<company_documents>\n{company}\n</company_documents>\n\n"
                    f"<evidence>\n{json.dumps(evidence, default=str)[:60000]}\n</evidence>"),
            effort="high", max_tokens=16000)
        j.bus.publish("display", {"title": f"Questionnaire answers{' - ' + buyer if buyer else ''}", "markdown": text})
        return text

    async def recruitment(self, role: str, notes: str | None = None) -> str:
        j = self.j
        text = await llm.write(
            j.client, j.settings, system=RECRUITMENT_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"role": role, "notes": notes, "staff_register": j.register.prompt_summary()},
                              default=str)[:40000],
            effort="medium", max_tokens=8000)
        j.bus.publish("display", {"title": f"Recruitment - {role}", "markdown": text})
        return text

    async def hr_letter(self, kind: str, person: str, details: str) -> str:
        j = self.j
        knowledge = j.kb.search(f"{kind} disciplinary employment law ACAS", limit=4)
        text = await llm.write(
            j.client, j.settings, system=HR_LETTER_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"letter_type": kind, "person": person, "details": details,
                               "knowledge_extracts": knowledge}, default=str)[:40000],
            effort="medium", max_tokens=8000)
        j.bus.publish("display", {"title": f"HR - {kind} ({person})", "markdown": text})
        return text

    async def bid_assessment(self, opportunity: str, value: float | None, notes: str | None = None) -> str:
        j = self.j
        business = await _safe(j.advisor.gather(), "business data")
        text = await llm.write(
            j.client, j.settings, system=BID_ASSESSMENT_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"opportunity": opportunity, "estimated_value": value, "notes": notes,
                               "business_data": business}, default=str)[:60000],
            effort="high", max_tokens=8000)
        j.bus.publish("display", {"title": f"Bid assessment - {opportunity}", "markdown": text})
        return text

    async def bid_document(self, opportunity: str, client: str | None, requirements: str,
                           notes: str | None = None) -> str:
        j = self.j
        evidence = {}
        for scheme in ("BAFE", "SSAIB", "CHAS"):
            try:
                evidence[scheme] = await j.accreditations.gather_evidence(scheme)
            except Exception as e:  # noqa: BLE001
                evidence[scheme] = {"error": str(e)[:200]}
        today = date.today()
        comparable_jobs = []
        try:
            jobs = await j.fsm.jobs(today - timedelta(days=730), today, status="completed")
            comparable_jobs = jobs[:15]  # a sample - the model picks what's actually relevant as case studies
        except Exception as e:  # noqa: BLE001
            comparable_jobs = [{"error": str(e)[:200]}]
        company = j.kb.core_documents()[:30000]
        text = await llm.write(
            j.client, j.settings, system=BID_DOCUMENT_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"opportunity": opportunity, "client": client, "requirements": requirements,
                               "notes": notes, "company_documents": company, "evidence": evidence,
                               "comparable_past_jobs": comparable_jobs}, default=str)[:70000],
            effort="high", max_tokens=16000)
        j.bus.publish("display", {"title": f"Bid document - {opportunity}", "markdown": text})
        return text
