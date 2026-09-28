"""Background schedule: routine tests, compliance sweep, inbox issue scan, morning briefing."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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
                  CronTrigger.from_crontab(s.compliance_check_cron, timezone=s.timezone), id="compliance",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("morning briefing", j.briefings.morning_briefing),
                  CronTrigger.from_crontab(s.briefing_cron, timezone=s.timezone), id="briefing",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("staff review", j.reviewer.weekly_review),
                  CronTrigger.from_crontab(s.staff_review_cron, timezone=s.timezone), id="staff_review",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("business review", j.business_review),
                  CronTrigger.from_crontab(s.business_review_cron, timezone=s.timezone), id="business_review",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("social snapshot", j.marketing.snapshot),
                  CronTrigger.from_crontab(s.social_snapshot_cron, timezone=s.timezone), id="social_snapshot",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("marketing report", j.marketing.weekly_report),
                  CronTrigger.from_crontab(s.marketing_report_cron, timezone=s.timezone), id="marketing_report",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("accreditation reminders", j.accreditations.daily_reminders),
                  CronTrigger.from_crontab("5 8 * * *", timezone=s.timezone), id="accreditations",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("regulatory watch", j.regwatch.weekly),
                  CronTrigger.from_crontab(s.regulatory_watch_cron, timezone=s.timezone), id="regwatch",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("billing check", j.daily_billing),
                  CronTrigger.from_crontab(s.billing_check_cron, timezone=s.timezone), id="billing",
                  max_instances=1, coalesce=True)
    sched.add_job(_guard("review requests", j.daily_reviews),
                  CronTrigger.from_crontab(s.review_requests_cron, timezone=s.timezone), id="reviews",
                  max_instances=1, coalesce=True)
    if not getattr(j.mail, "demo", True):
        sched.add_job(_guard("inbox scan", j.issues.scan_inbox), "interval", minutes=s.inbox_check_interval_min,
                      id="inbox_scan", max_instances=1, coalesce=True)
    return sched
