"""System schematics (services/schematics.py, schematic_layout.py, schematic_render.py, schematic_symbols.py; tools draw_schematic /
open_schematic / list_schematics; routes /api/schematics...).

Covered: spec validation for the three kinds (clear errors back to the model, caps, aliases, sanitised labels, no prices), the layout
(deterministic, no overlapping labels or symbols at typical sizes, inside the drawing, page breaks nothing crosses, assumed items grey
and dashed), the exports (SVG parses and escapes, PNG and PDF are real files, A4 / A3 page sizes, a big drawing splits over sheets,
the title block and the disclaimer), storage and revisions, the tools through dispatch and a full conversation turn (the reply event
carries the drawing reference, for the owner and for an engineer's own session), roles (engineer draws, office only opens / lists /
downloads), check mode (laid out, never saved) and the "What Jarvis did" lines.

Every fixture here is synthetic: no real customer, site or address."""
from __future__ import annotations

import asyncio
import io
import json
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from jarvis import access
from jarvis.access import Caller
from jarvis.brain import checkmode
from jarvis.brain.tools import TOOLS_BY_NAME, DrawSchematicIn, ListSchematicsIn, OpenSchematicIn, dispatch
from jarvis.core import Jarvis
from jarvis.events import EventBus
from jarvis.services import async_tools
from jarvis.services import schematic_layout as L
from jarvis.services import schematic_render as R
from jarvis.services import schematic_symbols as symbols
from jarvis.services import schematics as S
from tests.fakes import FakeClient, message, text_block, tool_block

TODAY = date(2026, 10, 9)
SAM = Caller(access.TEAM, "Sam", "eng1", access.ENGINEER)
PAT = Caller(access.TEAM, "Pat", "off1", access.OFFICE)
TYPES = ["smoke", "smoke", "heat", "mcp", "sounder_vad", "multi", "io", "vad", "beam", "sounder"]


def fire_spec(loops=2, per=24, **extra):
    out = {"title": "Fire alarm loops - Block A", "site": "Unit 4, Example Business Park", "source": "fsm", "source_ref": "FSM site 1001",
           "system_type": "addressable", "panel": {"label": "Main panel", "model": "Example 2-loop", "location": "Reception"},
           "panel_io": [{"label": "Fire signalling to ARC"}, {"label": "Plant shutdown relay", "assumed": True}],
           "network": [{"type": "repeater", "label": "Gatehouse repeater"}],
           "loops": [{"number": n + 1, "return_confirmed": n == 0,
                      "devices": [{"type": TYPES[i % len(TYPES)], "address": f"{i + 1:03d}", "zone": str(1 + i // 8),
                                   "label": f"Corridor {i + 1} first floor east wing", "isolator": i % 10 == 0, "assumed": i == 5}
                                  for i in range(per)]} for n in range(loops)],
           "assumptions": ["Addresses follow the order the devices were fitted"]}
    out.update(extra)
    return out


def conventional_spec():
    return {"title": "Conventional zones", "system_type": "conventional", "panel": {"label": "4-zone panel"},
            "zones": [{"number": z, "label": f"Floor {z}", "eol": "EOL 4k7", "devices": [{"type": "smoke", "label": f"Room {z}.{i}"} for i in range(5)]
                       + [{"type": "mcp", "label": "Stair exit"}, {"type": "sounder", "label": "Landing"}]} for z in range(1, 5)]}


def ce_spec(rows=8, cols=6):
    cats = ["sounders", "sounders", "door_holders", "plant_shutdown", "aov", "signalling", "lifts", "access_release"]
    return {"title": "Cause and effect - Block A", "site": "Unit 4, Example Business Park",
            "inputs": [{"id": f"Z{i}", "label": f"Zone {i} detectors, ground floor offices"} for i in range(1, rows + 1)],
            "outputs": [{"id": f"O{k}", "label": f"Output {k} {cats[k % len(cats)].replace('_', ' ')}", "category": cats[k % len(cats)]}
                        for k in range(1, cols + 1)],
            "effects": [{"input": f"Z{i}", "output": "O1", "action": "evacuate"} for i in range(1, rows + 1)]
            + [{"input": "Z1", "output": f"O{cols}", "action": "signal", "delay_s": 30, "assumed": True}]}


def net_spec(cams=8):
    return {"title": "Security systems - Block A", "source": "quote",
            "systems": [
                {"kind": "cctv", "label": "Car park", "nodes": [{"id": "nvr", "type": "nvr", "label": "NVR 16ch"},
                                                                {"id": "sw", "type": "poe_switch", "parent": "nvr", "link": "ethernet"}]
                 + [{"id": f"c{i}", "type": "camera", "parent": "sw", "link": "poe", "port": str(i), "label": f"Camera {i} car park"}
                    for i in range(1, cams + 1)]},
                {"kind": "access", "nodes": [{"id": "acu", "type": "controller", "label": "Door controller"},
                                             {"id": "r1", "type": "reader", "parent": "acu", "link": "osdp", "label": "Front door reader"},
                                             {"id": "l1", "type": "lock", "parent": "acu", "link": "relay", "assumed": True},
                                             {"id": "x1", "type": "exit_button", "parent": "acu"}]},
                {"kind": "intruder", "nodes": [{"id": "ias", "type": "intruder_panel", "label": "Intruder panel"},
                                               {"id": "kp", "type": "keypad", "parent": "ias", "link": "bus"},
                                               {"id": "exp", "type": "expander", "parent": "ias", "link": "bus"},
                                               {"id": "p1", "type": "pir", "parent": "exp", "link": "zone", "label": "Office PIR"},
                                               {"id": "dc1", "type": "door_contact", "parent": "exp", "link": "zone"}]},
                {"kind": "signalling", "nodes": [{"id": "stu", "type": "stu", "label": "Dual-path transmitter"},
                                                 {"id": "arc", "type": "arc", "parent": "stu", "link": "ip", "secondary_link": "4g"}]}]}


def make(settings, script=None):
    j = Jarvis(settings, client=FakeClient(script))
    j.schematics._today = lambda: TODAY
    return j


def problems(kind, spec):
    with pytest.raises(S.SchematicError) as e:
        S.validate(kind, spec)
    return " | ".join(e.value.problems)


# ============================================================================================ validation
@pytest.mark.parametrize("kind,spec", [("fire_loop", fire_spec()), ("fire_loop", conventional_spec()), ("cause_effect", ce_spec()),
                                       ("network", net_spec())])
def test_good_specs_validate_and_normalise(kind, spec):
    k, out = S.validate(kind, spec)
    assert k == kind and out["title"]
    assert json.loads(json.dumps(out)) == out          # plain JSON, storable as is


def test_kind_aliases_and_json_string_specs():
    assert S.validate("C&E", json.dumps(ce_spec()))[0] == "cause_effect"
    assert S.validate("cctv", net_spec())[0] == "network"
    assert "kind must be one of" in problems("floorplan", {})


def test_errors_name_the_place_and_suggest_the_fix():
    text = problems("fire_loop", {"title": "x", "loops": [{"devices": [{"type": "smok detector", "adress": "1"},
                                                                     {"type": "mcp", "address": "2"}, {"type": "heat", "address": "2"}]}],
                                  "zones": [{"devices": []}]})
    assert "loops[0].devices[0].type: 'smok detector' isn't a device type here - did you mean 'smoke'" in text
    assert "loops[0].devices[0].adress: unknown field - did you mean 'address'?" in text
    assert "address 2 is used twice on loop 1" in text
    assert "zones (with EOL) are for conventional systems" in text
    assert "title: is required" in problems("network", {"systems": [{"kind": "cctv", "nodes": [{"id": "a", "type": "camera"}]}]})


def test_aliases_for_device_types_are_understood():
    _, out = S.validate("fire_loop", {"title": "t", "loops": [{"devices": [{"type": "Manual Call Point"}, {"type": "beacon"},
                                                                          {"type": "multisensor"}, {"type": "I/O"}]}]})
    assert [d["type"] for d in out["loops"][0]["devices"]] == ["mcp", "vad", "multi", "io"]


def test_no_prices_anywhere():
    assert "no prices" in problems("fire_loop", fire_spec(title="Quote £4,250 + VAT"))
    assert "no prices" in problems("network", {"title": "t", "notes": ["Cost: 1200"],
                                               "systems": [{"kind": "cctv", "nodes": [{"id": "a", "type": "camera"}]}]})
    S.validate("network", {"title": "Costa coffee shop cameras", "systems": [{"kind": "cctv", "nodes": [{"id": "a", "type": "camera"}]}]})


def test_labels_are_plain_text_markup_and_secrets_stripped():
    _, out = S.validate("fire_loop", {"title": "<script>alert(1)</script>Loops", "loops": [{"devices": [
        {"type": "smoke", "label": "Hall <img src=x onerror=alert(1)>‮\u0000 east"}]}]})
    assert "<" not in json.dumps(out) and "‮" not in out["loops"][0]["devices"][0]["label"]


def test_caps_are_enforced_with_advice_to_split():
    assert "at most 250 fit" in problems("fire_loop", fire_spec(loops=1, per=251))
    assert "at most 8 fit" in problems("fire_loop", fire_spec(loops=9, per=2))
    assert "at most 30 fit" in problems("cause_effect", ce_spec(cols=31))
    many = {"title": "t", "systems": [{"kind": "cctv", "nodes": [{"id": "n0", "type": "nvr"}] +
                                       [{"id": f"c{i}", "type": "camera", "parent": "n0"} for i in range(70)]}]}
    assert "at most 64" in problems("network", many)
    assert "spec is too big" in problems("network", {"title": "t", "notes": ["x" * 400_000]})


def test_cause_effect_checks_ids_and_cells():
    spec = ce_spec()
    spec["effects"] += [{"input": "Z99", "output": "O1"}, {"input": "Z1", "output": "O1", "action": "alert"},
                        {"input": "Z2", "output": "O2", "action": "dance"}]
    text = problems("cause_effect", spec)
    assert "'Z99' isn't one of the inputs" in text and "Z1 -> O1 is given twice" in text and "'dance' isn't a action" in text
    _, out = S.validate("cause_effect", ce_spec())
    assert {e["code"] for e in out["effects"]} == {"C", "T30"}


def test_network_tree_checks():
    cyc = {"title": "t", "systems": [{"kind": "cctv", "nodes": [{"id": "a", "type": "nvr", "parent": "b"}, {"id": "b", "type": "switch", "parent": "a"}]}]}
    assert "connect round in a circle" in problems("network", cyc)
    cross = {"title": "t", "systems": [{"kind": "cctv", "nodes": [{"id": "a", "type": "nvr"}]},
                                       {"kind": "access", "nodes": [{"id": "b", "type": "controller", "parent": "a"}]}]}
    assert "isn't a node of another system" in problems("network", cross)
    dup = {"title": "t", "systems": [{"kind": "cctv", "nodes": [{"id": "a", "type": "nvr"}, {"id": "a", "type": "camera"}]}]}
    assert "'a' is used twice" in problems("network", dup)
    assert "needs a short code" in problems("network", {"title": "t", "systems": [{"kind": "other", "nodes": [{"id": "a", "type": "other"}]}]})


# ============================================================================================ layout
SPECS = [("fire_loop", fire_spec()), ("fire_loop", fire_spec(loops=3, per=60)), ("fire_loop", conventional_spec()),
         ("cause_effect", ce_spec()), ("cause_effect", ce_spec(rows=40, cols=24)), ("network", net_spec()), ("network", net_spec(cams=40))]


def _overlaps(scene):
    boxes = [(L.item_box(it), it) for it in scene["items"] if it["t"] in ("text", "sym")]
    bad = []
    for i, (a, ia) in enumerate(boxes):
        for b, ib in boxes[i + 1:]:
            if a[0] < b[2] - 0.5 and b[0] < a[2] - 0.5 and a[1] < b[3] - 0.5 and b[1] < a[3] - 0.5:
                bad.append((ia.get("s") or ia.get("k"), ib.get("s") or ib.get("k")))
    return bad


@pytest.mark.parametrize("mode", L.MODES)
@pytest.mark.parametrize("kind,spec", SPECS)
def test_layout_has_no_overlapping_labels_or_symbols_and_stays_inside(kind, spec, mode):
    _, s = S.validate(kind, spec)
    scene = L.layout(kind, s, mode)
    assert _overlaps(scene) == []
    for it in scene["items"]:
        x0, y0, x1, y1 = L.item_box(it)
        assert x0 >= -0.5 and y0 >= -0.5 and x1 <= scene["w"] + 0.5 and y1 <= scene["h"] + 0.5, it
    if mode == "narrow" and kind != "cause_effect":
        assert scene["w"] <= 340        # a phone, without zooming out


@pytest.mark.parametrize("kind,spec", SPECS)
def test_layout_is_deterministic(kind, spec):
    _, s1 = S.validate(kind, spec)
    _, s2 = S.validate(kind, json.loads(json.dumps(spec)))
    assert json.dumps(L.layout(kind, s1), sort_keys=True) == json.dumps(L.layout(kind, s2), sort_keys=True)


@pytest.mark.parametrize("kind,spec", SPECS)
def test_page_breaks_are_clean(kind, spec):
    """Nothing crosses a break, so a PDF can split there."""
    _, s = S.validate(kind, spec)
    scene = L.layout(kind, s)
    assert scene["breaks"] == sorted(scene["breaks"])
    for b in scene["breaks"]:
        crossing = [it for it in scene["items"] if L.item_box(it)[1] < b - 0.5 < b + 0.5 < L.item_box(it)[3]]
        assert crossing == [], (b, crossing[:3])


def test_fire_loop_draws_order_isolators_ends_and_assumed_items():
    _, s = S.validate("fire_loop", fire_spec(loops=2, per=24))
    scene = L.layout("fire_loop", s)
    syms = [it for it in scene["items"] if it["t"] == "sym"]
    texts = [it["s"] for it in scene["items"] if it["t"] == "text"]
    assert sum(1 for it in syms if it["k"] == "isolator") >= 3 * 2        # devices 1, 11, 21 on each loop (+ the legend)
    assert {"1A", "1B", "2A", "2B", "NET"} <= set(texts)
    assert "Return to the panel (B end) not confirmed" in texts             # loop 2's return isn't confirmed
    assumed = [it for it in syms if it["c"] == "assumed"]
    assert assumed and all(it["d"] == 1 for it in assumed)
    assert any(t.startswith("Assumed: Addresses follow") for t in texts) and L.DISCLAIMER in texts
    # devices in loop order: addresses read 001.. in the order they are drawn
    addr = [t.split(" · ")[0] for t in texts if t[:3].isdigit() and " · Z" in t]
    assert addr[:24] == [f"{i:03d}" for i in range(1, 25)]


def test_conventional_zones_end_in_an_eol_marker():
    _, s = S.validate("fire_loop", conventional_spec())
    scene = L.layout("fire_loop", s)
    assert sum(1 for it in scene["items"] if it["t"] == "sym" and it["k"] == "eol") == 4 + 1      # one per zone, plus the key
    assert "EOL 4k7" in [it["s"] for it in scene["items"] if it["t"] == "text"]


def test_every_symbol_expands_inside_its_box():
    for key in symbols.SYMBOLS:
        for p in symbols.expand(key, 100, 100, 40, code="AB"):
            x0, y0, x1, y1 = L.item_box(p) if p["t"] != "text" else (p["x"], p["y"], p["x"], p["y"])
            assert 79 <= x0 and x1 <= 121 and 79 <= y0 and y1 <= 121, (key, p)
    for t in symbols.FIRE_DEVICE_TYPES + symbols.FIRE_NETWORK_TYPES + symbols.SECURITY_NODE_TYPES:
        assert t in symbols.SYMBOLS


# ============================================================================================ exports
META = {"company": S.COMPANY, "title": "Fire alarm loops <b>&", "site": "Unit 4, Example Business Park", "system": "Fire alarm system",
        "job_ref": "J-0001", "number": "SCH-ABCDEF12", "rev": "P2", "date": "2026-10-09"}


@pytest.mark.parametrize("kind,spec", SPECS[:1] + SPECS[3:4] + SPECS[5:6])
def test_svg_export_parses_escapes_and_carries_the_title_block(kind, spec):
    _, s = S.validate(kind, spec)
    svg = R.to_svg_sheet(L.layout(kind, s), META)
    root = ET.fromstring(svg)
    texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
    for want in ("Salts Fire & Security", "Fire alarm loops <b>&", "SCH-ABCDEF12", "P2", "09/10/2026", "1 of 1", L.DISCLAIMER):
        assert want in texts, want
    assert "<script" not in svg and "<b>" not in svg and "onload" not in svg
    assert "complian" not in svg.lower()


def test_png_export_is_a_real_picture():
    from PIL import Image

    _, s = S.validate("network", net_spec())
    png = R.to_png_sheet(L.layout("network", s), META)
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.size[0] > 1000 and img.getpixel((5, 5)) == (255, 255, 255)
    assert len(set(img.convert("L").resize((64, 64)).getdata())) > 5        # something is drawn


@pytest.mark.parametrize("paper,size", [("a4", (842, 595)), ("a3", (1191, 842))])
def test_pdf_export_landscape_with_title_block_and_disclaimer(paper, size):
    from pypdf import PdfReader

    _, s = S.validate("fire_loop", fire_spec())
    pdf = R.to_pdf(L.layout("fire_loop", s), META, paper)
    reader = PdfReader(io.BytesIO(pdf))
    box = reader.pages[0].mediabox
    assert (round(float(box.width)), round(float(box.height))) == size
    text = reader.pages[0].extract_text()
    for want in ("Salts Fire & Security", "SCH-ABCDEF12", "Draft schematic prepared with Jarvis", "competent person"):
        assert want in text, want


def test_a_big_drawing_is_split_over_sheets_and_the_matrix_headings_repeat():
    from pypdf import PdfReader

    _, s = S.validate("fire_loop", fire_spec(loops=8, per=70))
    reader = PdfReader(io.BytesIO(R.to_pdf(L.layout("fire_loop", s), META, "a3")))
    n = len(reader.pages)
    assert 2 <= n <= R.MAX_SHEETS and f"{n} of {n}" in reader.pages[-1].extract_text()
    _, ce = S.validate("cause_effect", ce_spec(rows=60, cols=30))
    reader = PdfReader(io.BytesIO(R.to_pdf(L.layout("cause_effect", ce), META, "a4")))
    assert len(reader.pages) >= 2
    assert all("O1 Output 1" in p.extract_text() for p in reader.pages[:-1])


def test_pdf_survives_characters_helvetica_cannot_show():
    _, s = S.validate("fire_loop", {"title": "Ünïcode ✓ 中文", "loops": [{"devices": [{"type": "smoke", "label": "Café → 东"}]}]})
    assert R.to_pdf(L.layout("fire_loop", s), {**META, "title": "Ünïcode ✓"}, "a4").startswith(b"%PDF")


# ============================================================================================ storage, tools, roles
def test_save_revise_list_and_audit(settings):
    j = make(settings)
    k, s = S.validate("fire_loop", fire_spec())
    m1 = j.schematics.save(k, s, by="Alex")
    assert m1["rev"] == "P1" and m1["number"].startswith("SCH-") and m1["date"] == "2026-10-09"
    s2 = dict(s, title="Fire alarm loops - Block A (rev)")
    m2 = j.schematics.save(k, s2, drawing_id=m1["id"], note="moved the beam detector to loop 2", by="Alex")
    assert m2["rev"] == "P2" and m2["latest_revision"] == 2
    assert j.schematics.load(m1["id"], 1)[1]["spec"]["title"] == s["title"]
    assert [r["rev"] for r in j.schematics.revisions(m1["id"])] == ["P1", "P2"]
    assert j.schematics.list(site="Example Business")[0]["rev"] == "P2"
    with pytest.raises(S.SchematicError, match="keeps the same kind"):
        j.schematics.save("network", S.validate("network", net_spec())[1], drawing_id=m1["id"])
    with pytest.raises(S.SchematicError, match="no saved drawing"):
        j.schematics.save(k, s, drawing_id="0" * 12)
    data, mime, name = j.schematics.export(m1["id"], "pdf", 2, "a4", by="Sam (engineer)")
    assert mime == "application/pdf" and name.endswith("-P2-fire-alarm-loops-block-a-rev-a4.pdf") and data.startswith(b"%PDF")
    lines = [r["what"] for r in j.db.query("SELECT what FROM audit_events WHERE kind = 'schematic' ORDER BY id")]
    assert lines[0].startswith("Drew schematic SCH-") and "Revised schematic" in lines[1] and "moved the beam detector" in lines[1]
    assert "Exported schematic" in lines[2] and "PDF (A4)" in lines[2]
    from jarvis.services.activity_feed import Query

    since, until, _ = j.activity_feed.window("30d")
    page = j.activity_feed.page(Query(since, until, kinds=["draft"]))
    shown = [i for i in page["items"] if "schematic" in i["what"]]
    assert len(shown) == 3 and all(i["kind"] == "draft" for i in shown)


def test_draw_tool_saves_and_returns_a_reference_or_the_problems(settings):
    j = make(settings)
    tool = TOOLS_BY_NAME["draw_schematic"]
    assert tool.approval is False
    out = asyncio.run(dispatch(j, tool, DrawSchematicIn(kind="fire_loop", spec=fire_spec())))
    assert out["saved"] and out["drawing"]["rev"] == "P1" and "assumed" in out["summary"]
    bad = asyncio.run(dispatch(j, tool, DrawSchematicIn(kind="network", spec={"title": "t", "systems": [{"kind": "cctv", "nodes": [{"id": "a", "type": "camra"}]}]})))
    assert bad["drawn"] is False and "did you mean 'camera'" in bad["error"] and bad["spec_format"]
    rev = asyncio.run(dispatch(j, tool, DrawSchematicIn(kind="fire_loop", spec=fire_spec(), drawing_id=out["drawing"]["id"], change_note="tidy")))
    assert rev["drawing"]["rev"] == "P2"
    opened = asyncio.run(dispatch(j, TOOLS_BY_NAME["open_schematic"], OpenSchematicIn(drawing_id=out["drawing"]["id"], revision=1)))
    assert opened["found"] and opened["drawing"]["rev"] == "P1" and "_any_assumed" not in json.dumps(opened["spec"])
    assert S.validate("fire_loop", opened["spec"])                       # the opened spec can be edited and sent back as is
    listed = asyncio.run(dispatch(j, TOOLS_BY_NAME["list_schematics"], ListSchematicsIn(kind="fire_loop")))
    assert listed["count"] == 1 and listed["drawings"][0]["rev"] == "P2"


def test_check_mode_lays_out_but_never_saves(settings):
    j = make(settings)
    assert {"draw_schematic", "list_schematics", "open_schematic"} <= checkmode.CHECK_TOOLS
    out = asyncio.run(dispatch(j, TOOLS_BY_NAME["draw_schematic"], DrawSchematicIn(kind="cause_effect", spec=ce_spec()), check=True))
    assert out["drawn"] and out["saved"] is False and "drawing" not in out
    assert j.db.query("SELECT COUNT(*) AS n FROM schematics")[0]["n"] == 0
    token = checkmode.active.set(True)
    try:
        with pytest.raises(checkmode.CheckModeBlocked):
            j.schematics.save(*S.validate("cause_effect", ce_spec()))
    finally:
        checkmode.active.reset(token)


def test_roles_engineer_draws_office_views_and_downloads():
    assert {"draw_schematic", "open_schematic", "list_schematics"} <= access.TEAM_TOOLS
    assert access.tool_allowed("draw_schematic", SAM) and access.tool_allowed("open_schematic", SAM)
    assert not access.tool_allowed("draw_schematic", PAT)
    assert access.tool_allowed("open_schematic", PAT) and access.tool_allowed("list_schematics", PAT)
    assert "for the engineers and managers" in access.refusal("draw_schematic", PAT)
    for key in ("GET /api/schematics", "GET /api/schematics/{drawing_id}", "GET /api/schematics/{drawing_id}/export/{fmt}"):
        assert access.ROUTE_POLICY[key] == access.TEAM_OK
    assert {"draw_schematic", "open_schematic"} <= async_tools.NOT_BACKGROUND
    assert async_tools.is_untrusted_output("open_schematic") and async_tools.is_untrusted_output("list_schematics")


def test_office_is_refused_drawing_through_dispatch(settings):
    j = make(settings)
    out = asyncio.run(dispatch(j, TOOLS_BY_NAME["draw_schematic"], DrawSchematicIn(kind="fire_loop", spec=fire_spec()), caller=PAT))
    assert isinstance(out, str) and "engineers and managers" in out
    assert j.db.query("SELECT COUNT(*) AS n FROM schematics")[0]["n"] == 0
    out = asyncio.run(dispatch(j, TOOLS_BY_NAME["draw_schematic"], DrawSchematicIn(kind="fire_loop", spec=fire_spec()), caller=SAM))
    assert out["saved"] and j.db.query("SELECT created_by, created_role FROM schematics")[0] == {"created_by": "Sam (engineer)", "created_role": "team"}


def _turn(j, brain, bus):
    replies = []
    bus.add_tap(lambda t, d: replies.append(d) if t == "reply" else None)
    j.client.beta.messages.script += [message([tool_block("draw_schematic", {"kind": "network", "spec": net_spec()})], "tool_use"),
                                      message([text_block("Drawn - a draft for checking.")])]
    asyncio.run(brain.ask("draw the security layout", "typed"))
    return replies[-1]


def test_the_reply_carries_the_drawing_for_the_owners_console(settings):
    j = make(settings)
    reply = _turn(j, j.brain, j.bus)
    refs = reply.get("schematics")
    assert refs and refs[0]["rev"] == "P1" and refs[0]["kind"] == "network" and len(refs[0]["id"]) == 12


def test_the_reply_carries_the_drawing_for_an_engineers_own_session(settings):
    j = make(settings)
    session = j.team_sessions.get(SAM)
    reply = _turn(j, session.brain, session.bus)
    assert reply.get("schematics") and reply["schematics"][0]["title"] == "Security systems - Block A"


def test_the_routes_for_owner_engineer_and_office(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from jarvis.main import create_app

    monkeypatch.setattr("jarvis.main.LOGIN_DELAY_S", 0)
    settings.jarvis_owner_password = "owner-pass-1234"
    j = make(settings)
    meta = j.schematics.save(*S.validate("fire_loop", fire_spec()), by="Alex")
    app = create_app(settings, j)
    with TestClient(app):
        owner = TestClient(app)
        assert owner.post("/login", data={"password": "owner-pass-1234"}, follow_redirects=False).status_code == 303
        assert owner.post("/api/team-access/office", json={"code": "office-code-1111"}).status_code == 200
        assert owner.post("/api/team-access/engineer", json={"code": "engineer-code-2222"}).status_code == 200
        assert TestClient(app).get(f"/api/schematics/{meta['id']}").status_code == 401
        clients = {"owner": owner}
        for name, code in (("Pat", "office-code-1111"), ("Sam", "engineer-code-2222")):
            c = TestClient(app)
            assert c.post("/login/team", data={"name": name, "code": code}, follow_redirects=False).status_code == 303
            clients[name] = c
        for who, c in clients.items():
            assert c.get("/api/schematics").json()["drawings"][0]["id"] == meta["id"], who
            view = c.get(f"/api/schematics/{meta['id']}?mode=narrow").json()
            assert view["mode"] == "narrow" and view["scene"]["w"] <= 340 and not any(it["t"] == "sym" for it in view["scene"]["items"])
            r = c.get(f"/api/schematics/{meta['id']}/export/pdf?paper=a4")
            assert r.status_code == 200 and r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"], who
        assert owner.get(f"/api/schematics/{meta['id']}/export/docx").status_code == 404
        assert owner.get("/api/schematics/../../etc").status_code == 404
        assert owner.get("/api/schematics/zzzzzzzzzzzz").status_code == 404
        svg = clients["Sam"].get(f"/api/schematics/{meta['id']}/export/svg")
        assert svg.headers["content-type"].startswith("image/svg+xml") and svg.headers["x-content-type-options"] == "nosniff"
    exported_by = [r["actor"] for r in j.db.query("SELECT actor FROM audit_events WHERE what LIKE 'Exported schematic%'")]
    assert "Sam (engineer)" in exported_by and "Pat (office)" in exported_by


def test_the_console_script_builds_svg_with_dom_apis_only():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "schematics.js").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write", "DOMParser"):
        assert banned not in js, banned
    assert "textContent" in js and "validateScene" in js
    html = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "index.html").read_text(encoding="utf-8")
    assert "/static/schematics.js" in html and "/static/schematics.css" in html


def test_prompts_explain_facts_assumptions_and_roles(settings):
    from jarvis.brain.prompts import PERSONA, build_team_system

    assert "draw_schematic" in PERSONA and '"assumed": true' in PERSONA and "competent person" in PERSONA
    j = make(settings)
    eng = build_team_system(settings, j.kb, SAM)[0]["text"]
    off = build_team_system(settings, j.kb, PAT)[0]["text"]
    assert "draw_schematic" in eng and "list_schematics" in off and "for the engineers and managers" in off
