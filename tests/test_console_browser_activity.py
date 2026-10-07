"""Real-browser checks of the "What Jarvis did" pop-up (the Activity rail item).

Headless Chrome through Playwright (skipped when it is not installed), at 1280 / 800 / 400px in both themes. Pinned: the one-line
summary for today, the rows (status chip, kind, expandable cleaned detail, the error for a failure, who approved and when), the
quiet checks as ONE collapsed line, the filters (period, kind, status, who, search, "include everything"), server-side paging
("Show more"), the empty and error states in plain English, "Open in Approvals" (the only thing a row can do), Export CSV for the
owner, no secrets and no script execution from stored text, 44px touch targets and a full-screen drawer on a phone, and the look in
light and dark. Set JARVIS_SHOTS=<folder> to also save a screenshot of each surface there.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.sync_api")

from tests.test_console_browser import SIZES, THEMES, _no_hscroll, browser  # noqa: E402,F401
from tests.test_console_browser_phase3 import _page, _shot, serve  # noqa: E402,F401
from tests.test_console_browser_phase4a import SECRET, _open_pop  # noqa: E402


def _touch(width):
    return {"has_touch": True, "is_mobile": True} if width < 760 else {}


def _seed(j):
    mail = j.db.create_action("email_send", "Send the Kestrel quote to Dan", {"to": ["dan@kestrel.example.com"], "subject": "Quote Q-1042",
                                                                              "body": f"Hi Dan, the quote is attached. (Ref token {SECRET})"})
    j.db.decide_pending_action(mail, "approved", "Sam Taylor")
    j.db.set_action_status(mail, "done", "Email sent to dan@kestrel.example.com")
    waiting = j.db.create_action("email_send", "Chase Brightwell about quote Q-1180", {"to": ["pat@brightwell.example.com"], "subject": "Quote Q-1180",
                                                                                      "body": "Hi Pat, just checking the quote reached you."})
    declined = j.db.create_action("fsm_write", "Create customer 'Acme Alarms'", {"method": "POST", "path": "/customers", "body": {"name": "Acme Alarms"}})
    j.db.decide_pending_action(declined, "denied", "Priya", "Denied by Priya")
    failed = j.db.create_action("fsm_write", "Create site 'Unit 4'", {"method": "POST", "path": "/sites", "body": {"name": "Unit 4"}})
    j.db.set_action_status(failed, "failed", f"Salts FSM did not accept the change (HTTP 422): site name already exists ({SECRET})")
    auto = j.db.create_action("fsm_write", "Add a note to Acme", {"method": "POST", "path": "/notes", "body": {"text": "called"}},
                              status="approved", approved_by="standing approval: record_keeping")
    j.db.set_action_status(auto, "done", "Salts FSM updated: ok")
    for n in range(6):
        j.activity.record("pr_watch", "Pull request watch", "baseline" if n == 0 else "no_change", "No change.")
    j.activity.record("po_intake_scan", "Purchase order scan", "changed", "1 new.")
    j.activity_feed.record("settings", "the owner", "Changed settings: Voice")
    return {"mail": mail, "waiting": waiting, "declined": declined, "failed": failed, "auto": auto}


def _open_activity(page):
    _open_pop(page, "activity")
    page.wait_for_function("document.querySelectorAll('#activity-list .act-item').length > 0 || !document.getElementById('activity-empty').hidden", timeout=10000)


def _rows(page):
    return page.locator("#activity-list .act-item")


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_the_activity_pop_up_lists_what_jarvis_did(serve, browser, width, height, scheme):
    srv, j, _ = serve()
    ids = _seed(j)
    ctx, page = _page(browser, srv.url, width, height, scheme, **_touch(width))
    try:
        assert page.inner_text('.rail-item[data-pop="activity"]').strip() == "Activity"
        labels = page.evaluate("[...document.querySelectorAll('.rail-item')].map(b => b.dataset.pop)")
        assert labels[:2] == ["approvals", "activity"]
        _open_activity(page)
        assert page.inner_text("#drawer-title") == "What Jarvis did"
        summary = page.inner_text("#activity-summary")
        assert summary.startswith("Today: 5 proposed, 2 approved (1 automatically), 1 declined, 1 waiting, 1 failed, 2 other changes, 6 checks with nothing to report"), summary
        rows = _rows(page)
        # newest first; every row says what it is and has a status chip (text, not colour alone)
        statuses = rows.evaluate_all("els => els.map(e => e.dataset.status)")
        assert sorted(statuses) == sorted(["approved", "waiting", "declined", "failed", "auto_approved", "done", "done"]), statuses
        chips = {s: page.locator(f'.act-item[data-status="{s}"] .act-chip').first.inner_text() for s in set(statuses)}
        assert chips["auto_approved"] == "Auto-approved (standing)" and chips["waiting"] == "Waiting for you" and chips["failed"] == "Failed"
        # the quiet checks are ONE collapsed line, and are not rows
        quiet = page.locator("#activity-quiet-btn")
        assert quiet.inner_text().strip().startswith("6 checks with nothing to report")
        assert page.locator("#activity-quiet-list").is_hidden() and "Pull request watch" not in page.inner_text("#activity-list")
        quiet.click()
        assert page.locator("#activity-quiet-list").is_visible() and "Pull request watch" in page.inner_text("#activity-quiet-list")
        # a row expands to the cleaned detail: who approved, the error for a failure
        failed = page.locator(f'.act-item[data-id="action:{ids["failed"]}"]')
        failed.locator(".act-row").click()
        detail = failed.locator(".act-detail")
        assert detail.is_visible() and "HTTP 422" in detail.inner_text() and "What went wrong" in detail.inner_text()
        assert failed.locator(".act-row").get_attribute("aria-expanded") == "true"
        mail = page.locator(f'.act-item[data-id="action:{ids["mail"]}"]')
        mail.locator(".act-row").click()
        assert "Approved by Sam Taylor" in mail.locator(".act-detail").inner_text() and "Email" in mail.inner_text()
        # nothing secret reaches the page
        assert SECRET not in page.content() and SECRET not in page.inner_text("body")
        # the only thing a row can do is open Approvals
        assert page.locator("#pop-activity [data-act='approve'], #pop-activity [data-act='deny']").count() == 0
        # no horizontal scroll; the drawer fits; export is there for the owner
        assert _no_hscroll(page)[0] <= width
        fit = page.evaluate("() => { const d = document.getElementById('drawer'); return [d.scrollWidth <= d.offsetWidth + 1, d.offsetWidth <= innerWidth]; }")
        assert fit == [True, True]
        assert page.locator("#activity-export").is_visible()
        if width < 760:                                   # a phone: full screen, 44px targets
            box = page.eval_on_selector("#drawer", "e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height, innerWidth, innerHeight]; }")
            assert box[0] == 0 and box[1] == 0 and abs(box[2] - box[4]) <= 1 and abs(box[3] - box[5]) <= 1, box
            for sel in (".act-row", ".act-range", "#activity-kind", "#activity-status", "#activity-who", "#activity-search", "#activity-quiet-btn",
                        "#activity-export", ".act-toggle"):
                b = page.locator(sel).first.bounding_box()
                assert b["height"] >= 43.5, (sel, b)
        page.evaluate("document.getElementById('drawer-body').scrollTop = 0")
        _shot(page, f"activity-list-{width}-{scheme}")
        assert page.errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
def test_the_filters_search_and_the_include_everything_toggle(serve, browser, scheme):
    srv, j, _ = serve()
    ids = _seed(j)
    old = j.db.create_action("email_send", "Old chaser to Hartley", {"to": ["h@hartley.example.com"], "subject": "Old", "body": "old"})
    j.db.execute("UPDATE pending_actions SET created_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", old))
    from datetime import datetime, timedelta, timezone
    j.db.execute("UPDATE pending_actions SET created_at = ? WHERE id = ?", ((datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%S+00:00"), old))
    ctx, page = _page(browser, srv.url, 1280, 800, scheme)
    try:
        _open_activity(page)
        count = lambda: _rows(page).count()  # noqa: E731

        def settle():
            page.wait_for_timeout(500)
            page.wait_for_function("!document.getElementById('activity-more').disabled")

        total = count()
        assert total == 7
        page.click('.act-range[data-range="7d"]')
        page.wait_for_function(f"document.querySelectorAll('#activity-list .act-item').length === {total + 1}")
        assert page.get_attribute('.act-range[data-range="7d"]', "aria-pressed") == "true" and "Hartley" in page.inner_text("#activity-list")
        assert page.inner_text("#activity-summary").startswith("Today:")             # the summary line is always today's
        page.click('.act-range[data-range="today"]')
        page.wait_for_function(f"document.querySelectorAll('#activity-list .act-item').length === {total}")
        page.select_option("#activity-status", "needs_look")                      # failed + waiting
        page.wait_for_function("document.querySelectorAll('#activity-list .act-item').length === 2")
        assert sorted(_rows(page).evaluate_all("els => els.map(e => e.dataset.status)")) == ["failed", "waiting"]
        page.select_option("#activity-status", "")
        page.select_option("#activity-kind", "email")
        page.wait_for_function("document.querySelectorAll('#activity-list .act-item').length === 1")
        assert "Kestrel" in page.inner_text("#activity-list")
        page.select_option("#activity-kind", "")
        page.wait_for_function(f"document.querySelectorAll('#activity-list .act-item').length === {total}")
        who_options = page.eval_on_selector_all("#activity-who option", "els => els.map(e => e.value)")
        assert "Priya" in who_options and "Sam Taylor" in who_options and "Jarvis" in who_options
        page.select_option("#activity-who", "Priya")
        page.wait_for_function("document.querySelectorAll('#activity-list .act-item').length === 1")
        assert _rows(page).first.get_attribute("data-status") == "declined"
        page.select_option("#activity-who", "")
        page.fill("#activity-search", "brightwell")
        page.wait_for_function("document.querySelectorAll('#activity-list .act-item').length === 1")
        assert "Brightwell" in page.inner_text("#activity-list")
        page.fill("#activity-search", "zzzz nothing like this")
        page.wait_for_function("!document.getElementById('activity-empty').hidden")
        assert "Nothing matches those filters" in page.inner_text("#activity-empty")
        _shot(page, f"activity-empty-filtered-{scheme}")
        page.fill("#activity-search", "")
        page.wait_for_function(f"document.querySelectorAll('#activity-list .act-item').length === {total}")
        # include everything: the quiet checks come in as rows and the collapsed line goes
        page.check("#activity-everything")
        page.wait_for_function(f"document.querySelectorAll('#activity-list .act-item').length === {total + 6}")
        assert page.locator("#activity-quiet").is_hidden() and "Pull request watch: nothing to report" in page.inner_text("#activity-list")
        page.uncheck("#activity-everything")
        page.wait_for_function(f"document.querySelectorAll('#activity-list .act-item').length === {total}")
        assert page.locator("#activity-quiet").is_visible()
        assert page.errors == []
    finally:
        ctx.close()


def test_a_quiet_day_says_so_in_plain_english(serve, browser):
    srv, j, _ = serve()
    ctx, page = _page(browser, srv.url, 1280, 800, "dark")
    try:
        _open_pop(page, "activity")
        page.wait_for_function("!document.getElementById('activity-empty').hidden", timeout=10000)
        assert page.inner_text("#activity-empty") == "Nothing yet today. Jarvis hasn't proposed or changed anything, and nothing has failed."
        assert page.inner_text("#activity-summary") == "Today: nothing proposed or changed"
        assert page.locator("#activity-more").is_hidden() and page.locator("#activity-quiet").is_hidden()
        _shot(page, "activity-empty-dark")
    finally:
        ctx.close()


def test_the_list_loads_a_page_at_a_time_and_show_more_loads_the_rest(serve, browser):
    srv, j, _ = serve()
    stamp = "2026-01-01T00:00:00+00:00"
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    with j.db._lock:
        j.db._conn.executemany("INSERT INTO pending_actions (created_at, kind, summary, payload_json, status, result, decided_at) VALUES (?,?,?,?,?,?,?)",
                               [(stamp, "email_send", f"Bulk email {i}", json.dumps({"to": ["a@b.example.com"], "subject": f"S{i}", "body": "b"}), "pending", "", "")
                                for i in range(120)])
        j.db._conn.commit()
    ctx, page = _page(browser, srv.url, 400, 820, "light", **_touch(400))
    try:
        seen = []
        page.on("request", lambda r: seen.append(r.url) if "/api/activity?" in r.url else None)
        _open_activity(page)
        assert _rows(page).count() == 50 and page.locator("#activity-more").is_visible()
        assert page.locator("#activity-more").bounding_box()["height"] >= 43.5
        assert any("limit=50" in u and "offset=0" in u for u in seen)
        page.locator("#activity-more").scroll_into_view_if_needed()
        page.wait_for_function("document.querySelectorAll('#activity-list .act-item').length >= 100", timeout=10000)   # (scrolling to the end loads the next page)
        page.wait_for_function("!document.getElementById('activity-more').disabled")
        if page.locator("#activity-more").is_visible():
            page.click("#activity-more")
        page.wait_for_function("document.querySelectorAll('#activity-list .act-item').length === 120", timeout=10000)
        assert page.locator("#activity-more").is_hidden()
        ids = _rows(page).evaluate_all("els => els.map(e => e.dataset.id)")
        assert len(ids) == len(set(ids)) == 120
        assert max(int(u.split("offset=")[1].split("&")[0]) for u in seen) <= 100         # never asked for everything at once
        _shot(page, "activity-paged-400-light")
    finally:
        ctx.close()


def test_a_failed_load_says_so_and_try_again_works(serve, browser):
    srv, j, _ = serve()
    email = j.db.create_action("email_send", "Hello", {"to": ["a@b.example.com"], "subject": "S", "body": "b"})
    ctx, page = _page(browser, srv.url, 1280, 800, "dark")
    try:
        state = {"fail": True}

        def handler(route):
            if state["fail"]:
                route.fulfill(status=500, body="boom")
            else:
                route.continue_()

        page.route("**/api/activity?*", handler)
        _open_pop(page, "activity")
        page.wait_for_function("!document.getElementById('activity-error').hidden", timeout=10000)
        assert "Couldn't load what Jarvis did. Try again in a moment." in page.inner_text("#activity-error")
        _shot(page, "activity-error-dark")
        state["fail"] = False
        page.click("#activity-retry")
        page.wait_for_function("document.querySelectorAll('#activity-list .act-item').length === 1", timeout=10000)
        assert page.locator("#activity-error").is_hidden() and email
    finally:
        ctx.close()


def test_stored_text_is_never_run_as_script_and_a_waiting_row_opens_approvals(serve, browser):
    srv, j, _ = serve()
    j.db.create_action("email_send", "<img src=x onerror=\"window.__xss=1\">Chase <b>Bold</b>", {"to": ["a@b.example.com"], "subject": "<script>window.__xss=2</script>", "body": "<svg onload=window.__xss=3>"})
    ctx, page = _page(browser, srv.url, 1280, 800, "dark")
    try:
        _open_activity(page)
        page.locator("#activity-list .act-row").first.click()
        assert page.evaluate("window.__xss") is None
        assert page.locator("#activity-list img, #activity-list script, #activity-list svg, #activity-list b").count() == 0
        assert "<script>" in page.inner_text("#activity-list")                       # shown as text
        page.click('#activity-list [data-pop="approvals"]')
        page.wait_for_function("document.getElementById('drawer-title').textContent === 'Approvals'")
        assert page.locator("#pop-approvals").is_visible() and page.locator("#pop-activity").is_hidden()
        page.click('#pop-approvals [data-pop="activity"]')                            # and back, from the link at the foot of Approvals
        page.wait_for_function("document.getElementById('drawer-title').textContent === 'What Jarvis did'")
    finally:
        ctx.close()


def test_export_csv_link_carries_the_filters_and_downloads_a_clean_file(serve, browser):
    srv, j, _ = serve()
    _seed(j)
    ctx, page = _page(browser, srv.url, 1280, 800, "dark")
    try:
        _open_activity(page)
        page.select_option("#activity-status", "failed")
        page.wait_for_function("document.querySelectorAll('#activity-list .act-item').length === 1")
        href = page.get_attribute("#activity-export", "href")
        assert href.startswith("/api/activity/export.csv?") and "status=failed" in href and "range=today" in href
        text = page.evaluate("(h) => fetch(h, {credentials: 'same-origin'}).then(r => r.text())", href)
        assert text.count("\n") == 2 and "HTTP 422" in text and SECRET not in text      # header + one failed row, cleaned
        assert page.locator("#activity-export").get_attribute("download") is not None
    finally:
        ctx.close()
