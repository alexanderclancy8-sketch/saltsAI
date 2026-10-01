"""Background schedule: routine tests, compliance sweep, inbox issue scan, morning briefing."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..cron import cron_trigger

log = logging.getLogger(__name__)


def _guard(name: str, fn):
    async def run():
        try:
            await fn()
        except Exception:  # noqa: BLE001
            log.exception("Scheduled job %s failed", name)
    return run


def build_scheduler(j) -> AsyncIOScheduler:
    s = j.settings
    sched = AsyncIOScheduler(timezone=s.timezone)
    sched.add_job(_guard("system tests", lambda: j.tester.run("system")), "interval",
                  minutes=s.routine_test_interval_min, id="system_tests", max_instances=1, coalesce=True)
    sched.add_job(_guard("compliance", lambda: j.tester.run("compliance")),
                  cron_trigger(s.compliance_check_cron, timezone=s.timezone), id="compliance",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("morning briefing", j.briefings.morning_briefing),
                  cron_trigger(s.briefing_cron, timezone=s.timezone), id="briefing",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("staff review", j.reviewer.weekly_review),
                  cron_trigger(s.staff_review_cron, timezone=s.timezone), id="staff_review",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("business review", j.business_review),
                  cron_trigger(s.business_review_cron, timezone=s.timezone), id="business_review",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("social snapshot", j.marketing.snapshot),
                  cron_trigger(s.social_snapshot_cron, timezone=s.timezone), id="social_snapshot",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("marketing report", j.marketing.weekly_report),
                  cron_trigger(s.marketing_report_cron, timezone=s.timezone), id="marketing_report",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("accreditation reminders", j.accreditations.daily_reminders),
                  cron_trigger("5 8 * * *", timezone=s.timezone), id="accreditations",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("regulatory watch", j.regwatch.weekly),
                  cron_trigger(s.regulatory_watch_cron, timezone=s.timezone), id="regwatch",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("technical watch", j.regwatch.technical_weekly),
                  cron_trigger(s.technical_watch_cron, timezone=s.timezone), id="technical_watch",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("security watch", j.security_watch.run),
                  cron_trigger(s.security_watch_cron, timezone=s.timezone), id="security_watch",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("billing check", j.daily_billing),
                  cron_trigger(s.billing_check_cron, timezone=s.timezone), id="billing",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("review requests", j.daily_reviews),
                  cron_trigger(s.review_requests_cron, timezone=s.timezone), id="reviews",
                  max_instances=1, coalesce=True)
    if s.customer_comms_enabled:  # drafts only - every email still waits for the owner's approval
        sched.add_job(_guard("customer emails", j.customer_comms_sweep),
                      cron_trigger(s.customer_comms_cron, timezone=s.timezone), id="customer_comms",
                      max_instances=1, coalesce=True)
    sched.add_job(_guard("suggestions", j.suggestions.sweep),
                  cron_trigger(s.suggestions_cron, timezone=s.timezone), id="suggestions",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("end-of-day wrap-up", j.wrapup.run),
                  cron_trigger(s.wrapup_cron, timezone=s.timezone), id="wrapup",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("self-learning reflection", j.self_learning.reflect),
                  cron_trigger(s.self_learning_cron, timezone=s.timezone), id="self_learning",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("weekly digest", j.weekly_digest.scheduled),
                  cron_trigger(s.weekly_digest_cron, timezone=s.timezone), id="weekly_digest",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("lone-worker check", j.lone_worker_sweep), "interval",
                  minutes=s.lone_worker_check_min, id="lone_worker", max_instances=1, coalesce=True)
    if s.proactive_chat_enabled and j.self_github is not None:  # read-only; says what changed, only when it changed
        sched.add_job(_guard("pull request watch", j.proactive.pr_watch), "interval",
                      minutes=max(1, s.proactive_pr_watch_min), id="pr_watch", max_instances=1, coalesce=True)
    if not getattr(j.mail, "demo", True):
        sched.add_job(_guard("inbox scan", j.issues.scan_inbox), "interval", minutes=s.inbox_check_interval_min,
                      id="inbox_scan", max_instances=1, coalesce=True)
        sched.add_job(_guard("PO intake scan", j.po_intake.scan_inbox), "interval",
                      minutes=s.inbox_check_interval_min, id="po_intake_scan", max_instances=1, coalesce=True)
        sched.add_job(_guard("voicemail job intake", j.job_intake.scan_inbox), "interval",
                      minutes=s.inbox_check_interval_min, id="job_intake_scan", max_instances=1, coalesce=True)
    return sched
