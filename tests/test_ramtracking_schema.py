"""RAM Tracking against RAM's published schema (https://api.qaifn.co.uk/v2/api-docs), and the settings round trip.

The live failure after the sign-in was fixed was "'str' object has no attribute 'get'": RAM's VehicleStatusDTO has
``last_event`` as a plain string, and the connector read it as an object. Its tests had built the response by hand with an
object there, so they could never have caught it. These use responses shaped exactly like the published DTOs (and then
deliberately malformed ones), all mocked: nothing here has been run against RAM itself.
"""

from __future__ import annotations

import datetime as dt
import json
from urllib.parse import parse_qs

import httpx
import pytest

from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.services import connection_tests
from jarvis.settings_store import SettingsStore
from tests.fakes import FakeClient
from tests.test_ramtracking_connection import HISTORY, PARKED, SECRET, VEHICLE, Ram, jarvis


# --------------------------------------------------------------------------- the real cause: last_event is a string
async def test_the_connection_test_passes_against_a_response_shaped_exactly_like_rams_schema(settings):
    j = jarvis(settings, Ram(vehicles=lambda r: httpx.Response(200, json=[VEHICLE, PARKED])))
    ok, detail = await connection_tests.run(j, "ram")
    assert ok is True and "2 vehicles" in detail, detail
    assert "has no attribute" not in detail
    await j.http.aclose()


async def test_vehicles_and_positions_read_last_event_as_a_string(settings):
    j = jarvis(settings, Ram(vehicles=lambda r: httpx.Response(200, json=[VEHICLE, PARKED])))
    vehicles = await j.ram.vehicles()
    assert [(v["registration"], v["driver"], v["moving"]) for v in vehicles] == [
        ("YD71 SFS", "Dan Harper", True), ("YD72 SFS", "Priya Shah", False)]
    assert vehicles[0]["lat"] == 53.83 and vehicles[0]["timestamp"] == "2026-10-02T09:00:00Z"
    positions = await j.ram.positions()
    assert [(p["registration"], p["speed_mph"]) for p in positions] == [("YD71 SFS", 15), ("YD72 SFS", 0)]
    await j.http.aclose()


async def test_the_object_form_of_last_event_still_reads(settings):
    as_object = {**VEHICLE, "vehicle_status": {**VEHICLE["vehicle_status"], "last_event": {"event": "OVER_SPEED"}}}
    j = jarvis(settings, Ram(vehicles=lambda r: httpx.Response(200, json=[as_object])))
    assert (await j.ram.vehicles())[0]["moving"] is True
    await j.http.aclose()


async def test_journeys_read_the_published_history_shape(settings):
    j = jarvis(settings, Ram(history=lambda r: httpx.Response(200, json=HISTORY)))
    legs = await j.ram.journeys("101", dt.date(2026, 10, 1))
    assert len(legs) == 1
    assert legs[0]["start_address"] == "Home" and legs[0]["end_address"] == "Saltaire Court Offices"
    assert legs[0]["start_time"] == "2026-10-01T07:05:00Z" and legs[0]["end_lat"] == 53.83
    assert legs[0]["distance_miles"] is None
    await j.http.aclose()


async def test_one_vehicle_object_instead_of_a_list_is_read_as_a_one_vehicle_fleet(settings):
    j = jarvis(settings, Ram(vehicles=lambda r: httpx.Response(200, json=VEHICLE)))  # as the Swagger page shows it
    assert [v["registration"] for v in await j.ram.vehicles()] == ["YD71 SFS"]
    await j.http.aclose()


@pytest.mark.parametrize("bad", [
    "just a string", 42, None, ["nested"], {"vehicle_status": "oops"}, {"vehicle_status": {"location": "here"}},
    {"vehicle_driver": "Dan", "vehicle_status": {"last_event": ["a", "list"], "location": {"latitude": 1, "longitude": 2}}},
    {"vehicle_status": {"last_event": 7, "event_date": 5}},
])
async def test_a_malformed_vehicle_never_crashes_the_call_and_the_good_ones_survive(settings, bad):
    j = jarvis(settings, Ram(vehicles=lambda r: httpx.Response(200, json=[bad, VEHICLE, PARKED])))
    vehicles = await j.ram.vehicles()
    assert [v["registration"] for v in vehicles if v["registration"]] == ["YD71 SFS", "YD72 SFS"]
    positions = await j.ram.positions()
    assert {p["registration"] for p in positions} >= {"YD71 SFS", "YD72 SFS"}
    ok, detail = await connection_tests.run(j, "ram")
    assert ok is True and "has no attribute" not in detail
    await j.http.aclose()


@pytest.mark.parametrize("history", [
    {"history": "none"}, {"history": [None, "x", 3, {"event_name": 5}]}, {"history": None}, ["a", "b"], "text", None, 12,
    {"history": [{"event_name": {"odd": 1}, "event_date": 3}, {"event_name": "TRANSIT_STOP"}]},
])
async def test_malformed_history_gives_no_journeys_rather_than_an_error(settings, history):
    body = json.dumps(history).encode()  # (json=None would send no body at all; "null" is a real JSON answer)
    j = jarvis(settings, Ram(history=lambda r: httpx.Response(200, content=body, headers={"content-type": "application/json"})))
    assert await j.ram.journeys("101", dt.date(2026, 10, 1)) == []
    await j.http.aclose()


async def test_malformed_history_entries_do_not_spoil_the_good_ones(settings):
    messy = {"history": ["junk", None, *HISTORY["history"], {"event_name": ["x"]}]}
    j = jarvis(settings, Ram(history=lambda r: httpx.Response(200, json=messy)))
    assert len(await j.ram.journeys("101", dt.date(2026, 10, 1))) == 1
    await j.http.aclose()


# --------------------------------------------------------------------------- save -> load -> the request actually sent
@pytest.mark.parametrize("password", ["p!a&s+s%w=r#d", "15chars-pa$$w!", "q\"u'o<t>e s\\ £é", "  spaced out  "])
async def test_a_password_with_odd_characters_survives_save_reload_and_reaches_ram_exactly(tmp_path, password):
    def fresh():
        return Settings(data_dir=tmp_path / "data", scheduler_enabled=False, anthropic_api_key="t", _env_file=None)

    store = SettingsStore(fresh())
    assert store.update({"ram_client_id": "Alex Clancy", "ram_api_key": SECRET, "ram_username": "alex@example.com",
                         "ram_password": password}, []) == {}
    second = fresh()  # a brand-new process: nothing but the encrypted file on disk
    SettingsStore(second).apply()
    assert second.ram_password == password.strip() and second.ram_username == "alex@example.com"
    assert second.ram_api_key == SECRET and second.ram_client_id == "Alex Clancy"
    ram = Ram()
    http = httpx.AsyncClient(transport=httpx.MockTransport(ram))
    j = Jarvis(second, client=FakeClient(), http=http)
    await j.ram.vehicles()
    request = ram.log[0]
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(request.content.decode(), keep_blank_values=True) == {
        "grant_type": ["password"], "username": ["alex@example.com"], "password": [password.strip()]}
    await http.aclose()


async def test_leaving_a_secret_box_blank_keeps_what_was_saved(tmp_path):
    s = Settings(data_dir=tmp_path / "data", scheduler_enabled=False, anthropic_api_key="t", _env_file=None)
    store = SettingsStore(s)
    assert store.update({"ram_password": "real-password-1!"}, []) == {}
    assert store.update({"ram_password": "", "ram_username": "alex@example.com"}, []) == {}  # blank secret = keep it
    assert s.ram_password == "real-password-1!" and s.ram_username == "alex@example.com"
    view = store.view(type("Db", (), {"get_kv": staticmethod(lambda k: None)}), {"base_url": "https://x", "app_name": "x"})
    field = next(f for sec in view["sections"] for f in sec["fields"] if f["key"] == "ram_password")
    assert "real-password" not in json.dumps(field) and field["is_set"] is True


def test_the_settings_boxes_tell_the_browser_not_to_autofill_them():
    from pathlib import Path

    hud = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")
    assert "const NO_AUTOFILL" in hud and "data-lpignore" in hud and "data-1p-ignore" in hud
    assert 'autocomplete="new-password" ${NO_AUTOFILL}' in hud  # the secret boxes
    assert 'autocomplete="off" ${NO_AUTOFILL}' in hud  # the plain text boxes (an "API username" beside a password box)


# --------------------------------------------------------------------------- what the message says
async def test_the_message_says_what_jarvis_sent_as_lengths_never_values(settings):
    ram = Ram(token=lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad credentials"}))
    j = jarvis(settings, ram, ram_username="alex@example.com", ram_password="fifteen-chars!!")
    ok, detail = await connection_tests.run(j, "ram")
    assert "Jarvis sent a 16-character username, a 15-character password, a 11-character Client ID and a 17-character" in detail
    for secret in (SECRET, "fifteen-chars!!", "alex@example.com"):
        assert secret not in detail
    await j.http.aclose()


async def test_a_sign_in_answer_that_is_not_oauth_json_is_shown_in_part(settings):
    ram = Ram(token=lambda r: httpx.Response(400, text="<html><body><h1>Request blocked</h1> by the gateway</body></html>"))
    j = jarvis(settings, ram)
    ok, detail = await connection_tests.run(j, "ram")
    assert ok is False and "HTTP 400" in detail and "Request blocked by the gateway" in detail and "<h1>" not in detail
    await j.http.aclose()
