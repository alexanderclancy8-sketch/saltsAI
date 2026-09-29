"""Paperwork: risk assessments & method statements (RAMS) per job, tender / pre-qualification questionnaire
answers drafted from the company's real evidence, and HR documents (job postings, interview questions,
disciplinary/performance letters) grounded in the staff register and current UK employment law."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from ..brain import llm

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
