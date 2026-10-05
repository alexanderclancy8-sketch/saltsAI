"""Demo data is never reasoned from (console redesign phase 3, item 1).

Sources that are not connected yet (the accounts, the social figures, the stock records, the staff register, RAM
Tracking) show believable sample data in the console. These tests hold the line that none of it reaches the model as if
it were real: a tool built on sample data is withheld and says what to connect, a composite answer drops just that
section, the prompt never carries the sample staff, and the console's own pop-ups keep their demo data and labels.
"""

from __future__ import annotations

import json

import pytest

from jarvis import demo_guard
from jarvis.brain import prompts
from jarvis.brain.tools import TOOLS_BY_NAME, dispatch, serialise
from jarvis.core import Jarvis
from tests.fakes import FakeClient, message, text_block, tool_block


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


async def run_tool(j, tool_name, **args):
    tool = TOOLS_BY_NAME[tool_name]
    return await dispatch(j, tool, tool.model.model_validate(args))


def everything_sent_to_the_model(j) -> str:
    """Every prompt and system block of every call the fake Claude client received, as one string."""
    return json.dumps([(c.get("system"), c.get("messages")) for c in j.client.beta.messages.calls], default=str)


def sample_names(j) -> dict[str, set[str]]:
    """Names and figures that exist only in the sample data, read the way the console reads them (no tool call)."""
    out: dict[str, set[str]] = {}
    out["accounts"] = {"Kestrel Retail", "Bradford Council", "Aire Valley Care", "48213", "INV-10388"}
    out["stock"] = {i["name"] for i in j.stores.levels()["items"]} | {i["sku"] for i in j.stores.levels()["items"]}
    out["staff"] = {p["name"] for p in j.register.people()}
    out["socials"] = {"followers", "change_7d", "google_reviews"}
    return out


# --------------------------------------------------------------------------- every demo source is withheld
CASES = [
    ("finance_snapshot", {}, "accounts"),
    ("finance_aged", {}, "accounts"),
    ("finance_credit_control", {}, "accounts"),
    ("finance_vat", {}, "accounts"),
    ("finance_cashflow", {}, "accounts"),
    ("business_health", {}, "accounts"),
    ("customer_health", {}, "accounts"),
    ("contract_renewals", {}, "accounts"),
    ("marketing_overview", {}, "socials"),
    ("stock_levels", {}, "stock"),
    ("stock_reorder", {}, "stock"),
    ("stock_usage", {}, "stock"),
    ("stock_job_materials", {"job_ref": "J1"}, "stock"),
    ("staff_roles", {}, "staff"),
    ("staff_review", {}, "staff"),
    ("van_day", {"engineer": "anyone"}, "vehicles"),
]


@pytest.mark.parametrize("name,args,source", CASES)
async def test_a_tool_built_on_sample_data_is_withheld_and_says_what_to_connect(settings, name, args, source):
    j = make(settings)
    samples = sample_names(j)
    result = await run_tool(j, name, **args)
    assert isinstance(result, dict) and result["demo_data_withheld"] is True and result["tool"] == name
    assert [n["source"] for n in result["not_connected"]] == [demo_guard.SOURCES[result_source].label
                                                              for result_source in _sources_of(result)]
    assert demo_guard.SOURCES[source].label in {n["source"] for n in result["not_connected"]}
    # it names what needs connecting, in words the owner can act on
    assert "Connect" in result["instruction"] or "Enter" in result["instruction"] or "Tell me" in result["instruction"]
    assert demo_guard.SOURCES[source].connect in result["instruction"]
    # and carries nothing from the sample data
    text = serialise(result)
    for tokens in samples.values():
        for token in tokens:
            assert token not in text, (name, token)
    await j.http.aclose()


def _sources_of(result) -> list[str]:
    labels = {s.label: k for k, s in demo_guard.SOURCES.items()}
    return [labels[n["source"]] for n in result["not_connected"]]


async def test_the_same_tools_answer_normally_once_the_source_is_connected(settings):
    j = make(settings)
    j.finance.demo = False  # a connected accounts source (here the same ledger standing in for Sage)
    snapshot = await run_tool(j, "finance_snapshot")
    assert "demo_data_withheld" not in snapshot and "cash_at_bank" in snapshot
    await j.http.aclose()


async def test_the_model_is_told_in_the_tool_result_not_given_the_figures(settings):
    j = make(settings, [message([tool_block("finance_snapshot", {})], "tool_use"),
                        message([text_block("I can't give you the cash position yet - the accounts aren't connected.")])])
    reply = await j.brain.ask("How's cash?", "typed")
    result = j.brain.messages[2]["content"][0]["content"]
    assert "demo_data_withheld" in result and "cash_at_bank" not in result and "48213" not in result
    assert "Sage" in result  # what to connect
    assert "aren't connected" in reply
    await j.http.aclose()


# --------------------------------------------------------------------------- the console keeps its demo data and labels
async def test_the_console_still_shows_the_sample_data_with_its_labels(settings):
    j = make(settings)
    snapshot = await j.accountant.snapshot()  # not a tool call: nothing is withheld here
    assert snapshot["cash_at_bank"] and j.finance.demo
    assert (await j.marketing.overview(30))["demo"] is True
    assert j.stores.levels()["demo"] is True and j.register.people()
    conns = j.connections()
    for key in ("Accounts", "Socials / Google", "Stores / stock", "Staff register", "Vehicle tracking"):
        assert "DEMO" in conns[key], key
    await j.http.aclose()


# --------------------------------------------------------------------------- composites keep the real part
async def test_the_morning_briefing_drops_only_the_sample_section(settings):
    j = make(settings, [message([text_block("Morning. The accounts can't be covered yet - they aren't connected.")])])
    await run_tool(j, "morning_briefing")  # the briefing text is written from the data below
    prompt = everything_sent_to_the_model(j)
    assert "48213" not in prompt and "Kestrel" not in prompt and "cash_at_bank" not in prompt
    assert "Not connected" in prompt and "Sage" in prompt  # the stub tells the writer what to say instead
    assert "late_starts" in prompt  # the real parts (here the staff board from Salts FSM) are still there
    await j.http.aclose()


async def test_the_wrap_up_and_the_advisor_drop_only_the_sample_sections(settings):
    j = make(settings, [message([text_block("Wrap-up written.")]), message([text_block("Advice written.")])])
    await run_tool(j, "end_of_day_wrap_up")
    wrap = everything_sent_to_the_model(j)
    assert "48213" not in wrap and "Kestrel" not in wrap and "Not connected" in wrap
    await run_tool(j, "business_advice")
    advice = everything_sent_to_the_model(j)
    for sample in ("48213", "Kestrel", "Bradford Council", "followers"):
        assert sample not in advice, sample
    assert "Not connected" in advice
    await j.http.aclose()


async def test_suggestions_resting_on_sample_data_are_withheld_from_the_model_but_stay_in_the_console(settings):
    j = make(settings)
    await j.suggestions.sweep(announce=False)  # what the scheduler runs: builds them from the sample ledger too
    stored = {s["key"] for s in j.db.open_suggestions()}
    assert any(k.split(":")[0] in demo_guard.SUGGESTION_SOURCES for k in stored), stored
    seen = await run_tool(j, "suggestions")
    seen_keys = {s["key"] for s in seen}
    assert not any(k.split(":")[0] in demo_guard.SUGGESTION_SOURCES for k in seen_keys)
    assert {s["key"] for s in j.db.open_suggestions()} >= stored  # the panel still has them, labelled demo
    await j.http.aclose()


# --------------------------------------------------------------------------- the prompt
def test_the_prompt_says_to_treat_sample_data_as_absent_and_to_name_what_to_connect(settings):
    j = make(settings)
    blocks = prompts.build_system(settings, j.kb, j.db, j.connections(), j.register.prompt_summary())
    text = "\n".join(b["text"] for b in blocks)
    assert "Sample data is never an answer" in text
    assert "demo_data_withheld" in text and "what needs connecting" in text
    assert "DEMO" in text  # the connected-systems list marks each sample source


def test_the_sample_staff_register_is_not_put_in_the_prompt(settings):
    j = make(settings)
    names = [p["name"] for p in j.register.people()]
    assert names  # the console does show sample people
    summary = j.register.prompt_summary()
    for name in names:
        assert name not in summary
    blocks = prompts.build_system(settings, j.kb, j.db, j.connections(), summary)
    assert not any(name in b["text"] for b in blocks for name in names)
    assert "No real staff register" in summary


async def test_a_real_staff_register_is_used_and_the_first_person_does_not_drag_the_samples_in(settings):
    j = make(settings)
    sample = [p["name"] for p in j.register.people()]
    j.register.upsert("Jo Bloggs", role="Estimator", type_="office")
    assert not j.register.demo
    names = [p["name"] for p in j.register.people()]
    assert names == ["Jo Bloggs"] and not set(sample) & set(names)
    assert "Jo Bloggs" in j.register.prompt_summary()
    roles = await run_tool(j, "staff_roles")  # now a real answer, not a withheld one
    assert "demo_data_withheld" not in roles and roles["staff"][0]["name"] == "Jo Bloggs"
    await j.http.aclose()


# --------------------------------------------------------------------------- the mechanism itself
async def test_a_blanket_except_cannot_swallow_the_block_and_carry_on_with_sample_data(settings):
    j = make(settings)
    token = demo_guard.begin()
    try:
        try:
            await j.finance.invoices("receivable")
        except Exception:  # noqa: BLE001 - exactly the "one broken source must not spoil the briefing" pattern
            pytest.fail("DemoDataBlocked must not be an Exception")
    except demo_guard.DemoDataBlocked:
        pass
    finally:
        assert demo_guard.end(token) == ["accounts"]
    await j.http.aclose()


async def test_nothing_is_collected_or_blocked_outside_a_tool_call(settings):
    j = make(settings)
    assert not demo_guard.active()
    assert await j.finance.invoices("receivable")  # no error, no collector
    await j.http.aclose()


async def test_section_stubs_only_the_sample_part(settings):
    j = make(settings)

    async def real():
        return {"ok": 1}

    token = demo_guard.begin()
    try:
        assert await demo_guard.section(real()) == {"ok": 1}
        stubbed = await demo_guard.section(j.finance.invoices("receivable"))
        assert "error" in stubbed and "Not connected" in stubbed["error"] and stubbed["not_connected"]
        assert demo_guard.end(token) == []  # the section kept the outer tool call clean
    except BaseException:
        demo_guard.end(token)
        raise
    await j.http.aclose()


async def test_actions_that_change_things_still_queue_for_approval_and_are_untouched(settings):
    j = make(settings)
    result = await run_tool(j, "staff_update_role", name="Jo Bloggs", role="Estimator")
    assert "Suggested, not done" in result and "demo_data_withheld" not in json.dumps(result)
    assert j.db.pending_actions()  # queued for the owner, exactly as before
    await j.http.aclose()


async def test_the_suggestions_tool_still_refreshes_when_only_unrelated_sources_are_sample(settings, monkeypatch):
    j = make(settings)
    j.finance.demo = False  # accounts connected; the staff register, socials and vehicles are still sample data
    j.db.set_kv("stock_demo_seeded", "cleared")
    swept = []

    async def fake_sweep(announce=True):
        swept.append(announce)
        return [{"key": "unbilled", "title": "Invoice 3 completed jobs?"}]

    monkeypatch.setattr(j.suggestions, "sweep", fake_sweep)
    assert [s["key"] for s in await run_tool(j, "suggestions")] == ["unbilled"] and swept == [False]
    await j.http.aclose()
