"""Engineering-agent notifications (pull request ready, fix ready, deploy results, triage, security review) go to
Teams only by default: no email to the owner or anyone else, and a failed Teams post is surfaced, not re-routed."""

from __future__ import annotations

from jarvis.core import Jarvis
from jarvis.services.self_improve import SelfImprove, SubmitInput
from tests.fakes import FakeClient
from tests.test_self_improve import FakeGitHub, finding_files


class FakeTeams:
    def __init__(self, enabled=True, fail=False):
        self.enabled = enabled
        self.fail = fail
        self.posts: list[tuple[str, str]] = []

    async def post(self, title, body):
        if self.fail:
            raise RuntimeError("webhook 500")
        self.posts.append((title, body))


class FakeMail:
    demo = False

    def __init__(self):
        self.sent: list[tuple] = []

    async def send_mail(self, to, subject, body_html, cc=None, bcc=None, sensitivity=None):
        self.sent.append((to, subject))

    async def send_to_owner(self, to, subject, body_html):
        self.sent.append(([to], subject))
        return True, ""


def make(settings, teams=None, mail=None):
    settings.owner_email = "alex.clancy@saltsfireandsecurity.co.uk"
    j = Jarvis(settings, client=FakeClient())
    j.notifier.teams = teams or FakeTeams()
    j.notifier.mail = mail or FakeMail()
    return j


async def test_engineering_update_goes_to_teams_only_by_default(settings):
    j = make(settings)
    await j.notifier.notify("Pull request ready: X", "details", level="info", push=True, engineering=True)
    assert [t for t, _ in j.notifier.teams.posts] == ["Pull request ready: X"]
    assert j.notifier.mail.sent == []
    await j.http.aclose()


async def test_engineering_warning_pushes_to_teams_without_email(settings):
    j = make(settings)
    await j.notifier.notify("Security review failed", "boom", level="warning", engineering=True)
    assert len(j.notifier.teams.posts) == 1
    assert j.notifier.mail.sent == []
    await j.http.aclose()


async def test_non_engineering_push_still_uses_teams_and_email(settings):
    j = make(settings)
    await j.notifier.notify("Monthly report", "text", level="info", push=True)
    assert len(j.notifier.teams.posts) == 1
    assert len(j.notifier.mail.sent) == 1
    await j.http.aclose()


async def test_send_owner_update_for_non_engineering_is_unchanged(settings):
    j = make(settings)
    via = await j.notifier.send_owner_update("Hi", "there", channels=("teams", "email"))
    assert via == "Teams, email ('salts jarvis' folder)"  # owner-folder filing added by PR16
    await j.http.aclose()


async def test_email_can_be_enabled_in_the_channel_list(settings):
    j = make(settings)
    settings.engineering_notify_channels = "teams, email"
    await j.notifier.notify("Fix ready", "b", level="warning", push=True, engineering=True)
    assert len(j.notifier.teams.posts) == 1
    assert len(j.notifier.mail.sent) == 1
    await j.http.aclose()


async def test_garbage_channel_setting_falls_back_to_teams_only(settings):
    j = make(settings)
    settings.engineering_notify_channels = "sms, "
    assert j.notifier.engineering_channels() == ("teams",)
    await j.http.aclose()


async def test_teams_failure_is_not_silently_emailed_and_is_surfaced(settings):
    j = make(settings, teams=FakeTeams(fail=True))
    issue_id = j.db.create_issue(reporter="Sam", title="Bug", description="d", source="web", system="Salts FSM",
                                 severity="medium")
    await j.notifier.notify("Fix ready for issue", "b", level="warning", push=True, engineering=True,
                            issue_id=issue_id)
    assert j.notifier.mail.sent == []
    titles = [n["title"] for n in j.db.recent_notifications()]
    assert "Update not delivered to Teams" in titles
    assert "Teams delivery failed" in j.db.get_issue(issue_id)["notes"]
    await j.http.aclose()


async def test_teams_not_configured_is_surfaced_not_emailed(settings):
    j = make(settings, teams=FakeTeams(enabled=False))
    await j.notifier.notify("Fix ready", "b", level="warning", push=True, engineering=True)
    assert j.notifier.mail.sent == []
    assert "Update not delivered to Teams" in [n["title"] for n in j.db.recent_notifications()]
    await j.http.aclose()


async def test_email_fallback_only_when_explicitly_enabled(settings):
    j = make(settings, teams=FakeTeams(fail=True))
    settings.engineering_email_fallback = True
    await j.notifier.notify("Fix ready", "b", level="warning", push=True, engineering=True)
    assert len(j.notifier.mail.sent) == 1
    await j.http.aclose()


async def test_self_improve_pull_request_ready_is_held_for_weekly_digest(settings, monkeypatch):
    j = make(settings)
    gh = FakeGitHub(finding_files())
    si = SelfImprove(settings, j.db, j.bus, j.notifier, j.client, gh)
    outcome = {"kind": "submit", "fix": SubmitInput(pr_title="Add a tool", summary="Added.", test_notes="t",
                                                    risk="low")}

    async def fake_engineer(request, ws):
        ws.create("/repo/jarvis/new_tool.py", "# new tool\n")
        return outcome

    async def no_watch(*a, **k):
        pass

    monkeypatch.setattr(si, "_engineer", fake_engineer)
    monkeypatch.setattr(si, "watch_ci", no_watch)
    await si.run("add a new tool")

    # pr_ready is a digested kind (PR17): held for the weekly digest, not posted immediately.
    assert [t for t, _ in j.notifier.teams.posts] == []
    assert j.notifier.mail.sent == []
    assert [i["title"] for i in j.db.pending_digest_items()] == ["Pull request ready: Add a tool"]
    assert not gh.called("merge_pr")
    await j.http.aclose()


async def test_issue_triage_notification_is_teams_only(settings, monkeypatch):
    from jarvis.services.issues import Triage

    j = make(settings)
    issue = await j.issues.report(reporter="Sam", title="Photos hang", description="spinner", notify=False,
                                  process=False)

    async def fake_triage(_issue):
        return Triage(summary="Spinner hangs", category="data_problem", severity="high", software_fixable=False,
                      likely_area="uploads", suggested_next_steps=["Check storage"], reply_to_reporter="Thanks")

    calls = []
    real_notify = j.notifier.notify

    async def spy_notify(title, body="", **kw):
        calls.append((title, kw))
        await real_notify(title, body, **kw)

    monkeypatch.setattr(j.issues, "triage", fake_triage)
    monkeypatch.setattr(j.notifier, "notify", spy_notify)
    await j.issues.process(issue["id"])
    triaged = [kw for t, kw in calls if "triaged" in t]
    assert triaged and triaged[0]["engineering"] is True and triaged[0]["issue_id"] == issue["id"]
    assert j.notifier.mail.sent == []
    await j.http.aclose()
