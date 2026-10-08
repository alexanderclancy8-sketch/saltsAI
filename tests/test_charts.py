"""Chart specs: the closed vocabulary, the caps, hostile specs (NaN / infinity / giant arrays / HTML / control characters), the
`show_chart` tool, and - the privacy invariant - that a chart of owner-only data is published to the display marked for the
owner and is never delivered to a manager's or a team member's live connection."""

from __future__ import annotations

import contextlib
import copy
import json
import time

import pytest
from starlette.testclient import TestClient

from jarvis import access, auth
from jarvis.brain.tools import TOOLS_BY_NAME, ShowChartIn, dispatch
from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import charts
from jarvis.services.charts import ChartError, validate_chart
from tests.fakes import FakeClient
from tests.fsm_data_helpers import jarvis_with_fsm
from tests.test_fsm_analyse import MANAGER as MANAGER_CALLER
from tests.test_fsm_analyse import TEAM as TEAM_CALLER
from tests.test_fsm_analyse import make_api, run


def pts(*pairs):
    return [{"label": a, "value": b} for a, b in pairs]


def spec(ctype="bar", **kw):
    base = {"type": ctype, "title": "Jobs per engineer, 2026", "series": [{"name": "Jobs", "points": pts(("Dan", 41), ("Sam", 38.5), ("Priya", 12))}]}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- the good ones
@pytest.mark.parametrize("ctype", ["bar", "line", "pie", "donut", "stacked_bar"])
def test_each_chart_type_validates_and_normalises(ctype):
    out = validate_chart(spec(ctype, x_label="Engineer", y_label="Jobs", unit="number"))
    assert out["type"] == ctype and out["title"] == "Jobs per engineer, 2026" and out["unit"] == "number"
    assert out["x_label"] == "Engineer" and out["y_label"] == "Jobs"
    assert out["series"] == [{"name": "Jobs", "points": pts(("Dan", 41), ("Sam", 38.5), ("Priya", 12))}]
    assert set(out) == {"type", "title", "x_label", "y_label", "unit", "series"}


def test_a_multi_series_line_and_stacked_chart():
    two = [{"name": "2025", "points": pts(("Jan", 3), ("Feb", 4))}, {"name": "2026", "points": pts(("Jan", 5), ("Feb", 6))}]
    for t in ("line", "stacked_bar"):
        out = validate_chart(spec(t, series=two, unit="gbp"))
        assert [s["name"] for s in out["series"]] == ["2025", "2026"] and out["unit"] == "gbp"


def test_money_is_kept_to_pence_and_other_numbers_to_four_places():
    out = validate_chart(spec(unit="gbp", series=[{"name": "x", "points": pts(("a", 1.005), ("b", 2.3456789))}]))
    assert [p["value"] for p in out["series"][0]["points"]] == [1.0, 2.35]
    out = validate_chart(spec(series=[{"name": "x", "points": pts(("a", 1 / 3))}]))
    assert out["series"][0]["points"][0]["value"] == 0.3333


def test_numeric_strings_and_decimals_are_read_but_not_other_text():
    from decimal import Decimal

    out = validate_chart(spec(series=[{"name": "x", "points": pts(("a", "1,250.5"), ("b", "£99"), ("c", Decimal("2.5")), ("d", 7))}]))
    assert [p["value"] for p in out["series"][0]["points"]] == [1250.5, 99.0, 2.5, 7.0]


def test_names_default_sensibly_and_duplicates_are_kept_apart():
    out = validate_chart(spec("line", series=[{"points": pts(("a", 1))}, {"name": "", "points": pts(("a", 2))}, {"name": "S", "points": pts(("a", 3))},
                                              {"name": "S", "points": pts(("a", 4))}]))
    assert [s["name"] for s in out["series"]] == ["Series 1", "Series 2", "S", "S (2)"]
    assert validate_chart(spec(series=[{"points": pts(("a", 1))}]))["series"][0]["name"] == "Jobs per engineer, 2026"


def test_a_model_object_is_accepted_as_well_as_a_dict():
    m = ShowChartIn.model_validate({"type": "Bar", "title": "T", "series": [{"name": "n", "points": [{"label": 2026, "value": 3}]}]})
    assert validate_chart(m)["series"][0]["points"] == [{"label": "2026", "value": 3.0}]


# --------------------------------------------------------------------------- the caps
@pytest.mark.parametrize("ctype,n_ok,n_bad", [("bar", 24, 25), ("line", 60, 61), ("pie", 12, 13), ("donut", 12, 13), ("stacked_bar", 24, 25)])
def test_the_point_caps(ctype, n_ok, n_bad):
    mk = lambda n: spec(ctype, series=[{"name": "s", "points": pts(*[(f"L{i}", i + 1) for i in range(n)])}])
    assert len(validate_chart(mk(n_ok))["series"][0]["points"]) == n_ok
    with pytest.raises(ChartError) as e:
        validate_chart(mk(n_bad))
    assert str(n_ok) in str(e.value) and "Other" in str(e.value)


def test_the_series_caps():
    one = lambda i: {"name": f"s{i}", "points": pts(("a", 1))}
    for t, ok, bad in (("bar", 1, 2), ("pie", 1, 2), ("donut", 1, 2), ("line", 6, 7), ("stacked_bar", 8, 9)):
        assert validate_chart(spec(t, series=[one(i) for i in range(ok)]))
        with pytest.raises(ChartError, match="at most"):
            validate_chart(spec(t, series=[one(i) for i in range(bad)]))


def test_the_total_points_cap_and_the_label_union_cap():
    big = [{"name": f"s{i}", "points": pts(*[(f"L{i}_{k}", 1) for k in range(24)])} for i in range(8)]
    with pytest.raises(ChartError, match="different labels|Too many"):
        validate_chart(spec("stacked_bar", series=big))
    many = [{"name": f"s{i}", "points": pts(*[(f"L{k}", 1) for k in range(60)])} for i in range(5)]
    with pytest.raises(ChartError, match="Too many points"):
        validate_chart(spec("line", series=many))


def test_giant_input_is_refused_quickly_without_building_anything_big():
    t0 = time.perf_counter()
    with pytest.raises(ChartError):
        validate_chart(spec(series=[{"name": "s", "points": pts(*[(f"L{i}", 1) for i in range(300_000)])}]))
    with pytest.raises(ChartError):
        validate_chart(spec("line", series=[{"name": "s", "points": pts(("a", 1))}] * 100_000))
    assert time.perf_counter() - t0 < 2.0
    out = validate_chart(spec(title="T" * 5_000_000, series=[{"name": "N" * 1_000_000, "points": pts(("L" * 2_000_000, 1))}]))
    assert len(out["title"]) <= 121 and len(out["series"][0]["name"]) <= 41 and len(out["series"][0]["points"][0]["label"]) <= 61


# --------------------------------------------------------------------------- hostile specs
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 1e16, -1e300, 10 ** 400, "NaN", "inf", "-Infinity", "1e999", "abc", "",
                                 None, True, False, [1], {"v": 1}, object()])
def test_non_finite_huge_or_non_numeric_values_are_refused(bad):
    with pytest.raises(ChartError):
        validate_chart(spec(series=[{"name": "s", "points": [{"label": "a", "value": bad}]}]))


def test_the_biggest_allowed_value_passes():
    assert validate_chart(spec(series=[{"name": "s", "points": pts(("a", 1e15), ("b", -1e15))}]))


@pytest.mark.parametrize("bad,frag", [
    ({"type": "scatter"}, "Chart type"), ({"type": ""}, "Chart type"), ({"type": None}, "Chart type"), ({"type": "__proto__"}, "Chart type"),
    ({"type": "<script>"}, "Chart type"), ({"type": 7}, "Chart type"), ({"title": ""}, "title"), ({"title": "   "}, "title"), ({"title": None}, "title"),
    ({"title": "<b></b>"}, "title"), ({"unit": "usd"}, "unit"), ({"series": []}, "series"), ({"series": None}, "series"), ({"series": "abc"}, "series"),
    ({"series": {"a": 1}}, "series"), ({"series": ["abc"]}, "Series 1"), ({"series": [None]}, "Series 1"), ({"series": [[1, 2]]}, "Series 1"),
    ({"series": [{"name": "s"}]}, "at least one point"), ({"series": [{"name": "s", "points": []}]}, "at least one point"),
    ({"series": [{"name": "s", "points": "abc"}]}, "at least one point"), ({"series": [{"name": "s", "points": ["a"]}]}, "object with label"),
    ({"series": [{"name": "s", "points": [None]}]}, "object with label"), ({"series": [{"name": "s", "points": [[1, 2]]}]}, "object with label"),
    ({"series": [{"name": "s", "points": [{"label": "", "value": 1}]}]}, "label"), ({"series": [{"name": "s", "points": [{"value": 1}]}]}, "label"),
    ({"series": [{"name": "s", "points": [{"label": "<i></i>", "value": 1}]}]}, "label"), ({"series": [{"name": "s", "points": [{"label": "a"}]}]}, "value"),
    ({"series": [{"name": "s", "points": pts(("a", 1), ("a", 2))}]}, "twice"),
])
def test_malformed_specs_are_refused_with_a_sentence(bad, frag):
    with pytest.raises(ChartError) as e:
        validate_chart({**spec(), **bad})
    assert frag.lower() in str(e.value).lower()


def test_a_spec_that_is_not_an_object_is_refused():
    for bad in (None, "bar", 1, [spec()], 3.5):
        with pytest.raises(ChartError):
            validate_chart(bad)


def test_negative_values_only_where_they_make_sense():
    neg = [{"name": "s", "points": pts(("a", -3), ("b", 5))}]
    assert validate_chart(spec("bar", series=neg)) and validate_chart(spec("line", series=neg))
    for t in ("pie", "donut", "stacked_bar"):
        with pytest.raises(ChartError, match="negative"):
            validate_chart(spec(t, series=neg))
    with pytest.raises(ChartError, match="more than zero"):
        validate_chart(spec("pie", series=[{"name": "s", "points": pts(("a", 0), ("b", 0))}]))


def test_html_and_control_characters_are_stripped_from_every_text():
    evil = "<img src=x onerror=alert(1)>Dan<script>steal()</script>\x00\x07‮​"
    out = validate_chart({"type": "bar", "title": f"T {evil}", "x_label": evil, "y_label": evil, "unit": "number",
                          "series": [{"name": evil, "points": [{"label": evil, "value": 1}]}]})
    blob = json.dumps(out, ensure_ascii=True)
    for bad in ("<img", "<script", "onerror=", "</", "\\u0000", "\\u0007", "\\u202e", "\\u200b", ">"):
        assert bad not in blob, bad
    assert out["series"][0]["points"][0]["label"].startswith("Dan")


def test_extra_keys_are_dropped_and_the_input_is_not_mutated():
    s = spec()
    s["__proto__"] = {"polluted": True}
    s["series"][0]["onclick"] = "alert(1)"
    s["series"][0]["points"][0]["href"] = "javascript:alert(1)"
    before = copy.deepcopy(s)
    out = validate_chart(s)
    assert s == before and "__proto__" not in out and "onclick" not in out["series"][0] and "href" not in out["series"][0]["points"][0]


def test_secret_looking_text_in_a_label_is_redacted():
    out = validate_chart(spec(series=[{"name": "s", "points": pts(("sk-ant-api03-" + "A" * 40, 1))}]))
    assert "sk-ant" not in out["series"][0]["points"][0]["label"]


def test_spec_text_and_categories_helpers():
    out = validate_chart(spec("stacked_bar", series=[{"name": "A", "points": pts(("x", 1.5), ("y", 2))}, {"name": "B", "points": pts(("y", 3), ("z", 4))}]))
    assert charts.categories(out) == ["x", "y", "z"]
    text = charts.spec_text(out)
    assert "1.5" in text and "1.50" in text and "Jobs per engineer" in text


# --------------------------------------------------------------------------- the tool
@pytest.fixture
async def env(settings):
    j, _ = jarvis_with_fsm(settings, make_api())
    yield j
    await j.http.aclose()


async def show(j, args, caller=None):
    t = TOOLS_BY_NAME["show_chart"]
    return await dispatch(j, t, t.model.model_validate(args), caller=caller)


def events(q, kind="display"):
    out = []
    while not q.empty():
        m = q.get_nowait()
        if m["type"] == kind:
            out.append(m["data"])
    return out


def test_show_chart_is_read_only_ungated_and_the_schema_lists_the_vocabulary():
    t = TOOLS_BY_NAME["show_chart"]
    assert t.approval is False
    props = t.definition()["input_schema"]["properties"]
    assert set(props) == {"type", "title", "series", "x_label", "y_label", "unit", "owner_only"}
    assert props["unit"]["enum"] == list(charts.UNITS)


async def test_show_chart_publishes_a_validated_spec_to_the_display(env):
    j = env
    q = j.bus.subscribe()
    out = await show(j, {"type": "BAR", "title": "Jobs <b>per</b> engineer", "unit": "number",
                         "series": [{"name": "Jobs", "points": [{"label": "Dan", "value": 41}, {"label": "Sam", "value": 38}]}]})
    assert out == {"shown": True, "type": "bar", "points": 2, "owner_only": False}
    (ev,) = events(q)
    assert ev["chart"]["title"] == "Jobs per engineer" and ev["chart"]["type"] == "bar" and "audience" not in ev
    assert j.db.pending_actions() == []


async def test_show_chart_refuses_a_bad_spec_and_publishes_nothing(env):
    j = env
    q = j.bus.subscribe()
    for args in ({"type": "bar", "title": "T", "series": [{"name": "s", "points": [{"label": "a", "value": float("nan")}]}]},
                 {"type": "radar", "title": "T", "series": [{"name": "s", "points": [{"label": "a", "value": 1}]}]},
                 {"type": "bar", "title": "T", "series": [{"name": "s", "points": [{"label": "<p></p>", "value": 1}]}]},
                 {"type": "bar", "title": "T", "series": [{"name": "s", "points": [{"label": f"L{i}", "value": i} for i in range(25)]}]}):
        out = await show(j, args)
        assert out["shown"] is False and out["error"]
    assert events(q) == []


async def test_owner_only_charts_are_marked_and_a_manager_cannot_make_one(env):
    j = env
    q = j.bus.subscribe()
    args = {"type": "donut", "title": "Payroll by team", "owner_only": True, "unit": "gbp",
            "series": [{"name": "Pay", "points": [{"label": "Fitters", "value": 31000}, {"label": "Office", "value": 12500}]}]}
    out = await show(j, args)
    assert out["shown"] and out["owner_only"] is True
    assert events(q)[0]["audience"] == "owner"
    refused = await show(j, args, caller=MANAGER_CALLER)
    assert refused["shown"] is False and "owner" in refused["error"] and events(q) == []
    assert await show(j, args, caller=TEAM_CALLER) == access.refusal("show_chart")


async def test_numbers_read_from_owner_only_data_are_charted_for_the_owner_alone_even_if_not_flagged(env):
    j = env
    await run(j, {"resource": "invoices", "group_by": ["customer"], "metrics": ["sum(total)"]})          # 49214 / 250 are now owner-only figures
    q = j.bus.subscribe()
    args = {"type": "bar", "title": "Who owes us", "unit": "gbp",
            "series": [{"name": "Owed", "points": [{"label": "Kestrel Ltd", "value": 49214.0}, {"label": "Moorside", "value": 250}]}]}
    assert (await show(j, args))["owner_only"] is True and events(q)[0]["audience"] == "owner"
    refused = await show(j, args, caller=MANAGER_CALLER)
    assert refused["shown"] is False and events(q) == []
    fine = {"type": "bar", "title": "Visits", "series": [{"name": "n", "points": [{"label": "Mon", "value": 3}, {"label": "Tue", "value": 5}]}]}
    assert (await show(j, fine, caller=MANAGER_CALLER))["shown"] is True


async def test_charting_owner_only_numbers_means_they_cannot_be_remembered(env):
    j = env
    await show(j, {"type": "bar", "title": "Pay", "owner_only": True, "unit": "gbp",
                   "series": [{"name": "p", "points": [{"label": "Dan", "value": 3120.5}]}]})
    rem = TOOLS_BY_NAME["remember"]
    assert "Not remembered" in await dispatch(j, rem, rem.model.model_validate({"fact": "Dan's gross is 3120.50"}))


# --------------------------------------------------------------------------- the invariant: who is sent a sensitive chart
OWNER_PW = "owner-pass-1234"
TEAM_CODE = "team-code-5678"
MGR = "manager@salts.example"


def _cookie(client, name):
    return {"cookie": f"{name}={client.cookies[name]}"}


def test_an_owner_only_chart_reaches_the_owners_console_and_no_one_elses(settings, monkeypatch):
    monkeypatch.setattr("jarvis.main.LOGIN_DELAY_S", 0)
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    settings.jarvis_owner_password = OWNER_PW
    settings.manager_emails = MGR
    j = Jarvis(settings, client=FakeClient())
    app = create_app(settings, j)
    mgr_headers = {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": MGR}
    with TestClient(app) as live:
        owner_c = TestClient(app)
        assert owner_c.post("/login", data={"password": OWNER_PW}, follow_redirects=False).status_code == 303
        assert owner_c.post("/api/team-access", json={"code": TEAM_CODE}).status_code == 200
        team_c = TestClient(app)
        assert team_c.post("/login/team", data={"name": "Sam", "code": TEAM_CODE}, follow_redirects=False).status_code == 303
        assert TestClient(app).get("/api/me", headers=mgr_headers).json()["role"] == "manager"

        secret = {"title": "Payroll", "markdown": "", "chart": validate_chart(spec(title="Payroll by team")), "audience": "owner"}
        public = {"title": "Visits", "markdown": "", "chart": validate_chart(spec(title="Visits by day"))}

        with live.websocket_connect("/ws", headers=_cookie(owner_c, auth.COOKIE)) as owner_ws, \
                live.websocket_connect("/ws", headers=mgr_headers) as mgr_ws, \
                live.websocket_connect("/ws", headers=_cookie(team_c, auth.TEAM_COOKIE)) as team_ws:
            live.portal.call(j.bus.publish, "display", secret)
            live.portal.call(j.bus.publish, "display", public)
            live.portal.call(j.bus.publish, "reload", {"reason": "settings"})        # a signal every console gets: the end of the run
            owner_seen = [owner_ws.receive_json() for _ in range(3)]
            assert [m["type"] for m in owner_seen] == ["display", "display", "reload"]
            assert owner_seen[0]["data"]["chart"]["title"] == "Payroll by team"
            mgr_seen = [mgr_ws.receive_json() for _ in range(2)]
            assert [m["type"] for m in mgr_seen] == ["display", "reload"]
            assert mgr_seen[0]["data"]["chart"]["title"] == "Visits by day" and "Payroll" not in json.dumps(mgr_seen)
            team_seen = team_ws.receive_json()
            assert team_seen["type"] == "reload" and "Payroll" not in json.dumps(team_seen)


def test_event_visible_rule():
    secret = {"type": "display", "data": {"audience": "owner", "chart": {}}}
    assert access.event_visible(secret, access.OWNER) and not access.event_visible(secret, access.MANAGER)
    assert not access.event_visible(secret, access.TEAM) and not access.event_visible(secret, None)
    for ev in ({"type": "display", "data": {"title": "x"}}, {"type": "reply", "data": {"text": "x"}}, {"type": "stopped", "data": None},
               {"type": "display", "data": {"audience": "everyone"}}, "junk", None):
        assert access.event_visible(ev, access.MANAGER) and access.event_visible(ev, access.TEAM)


async def test_an_owner_only_analysis_chart_is_marked_by_fsm_analyse_too(env):
    j = env
    q = j.bus.subscribe()
    await run(j, {"resource": "payslips", "metrics": ["sum(gross)"], "group_by": ["employee"], "chart": "bar"})
    (ev,) = events(q)
    assert ev["audience"] == "owner" and access.event_visible({"type": "display", "data": ev}, access.OWNER)
    assert not access.event_visible({"type": "display", "data": ev}, access.MANAGER)
