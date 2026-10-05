"""RAM Tracking: why the live app showed a 404 / "credentials not detected", and what the app now does instead
(console redesign phase 3, item 4).

Everything here is against a mocked RAM (httpx.MockTransport). None of it has been, or could be, run against RAM's real API:
there are no credentials in the test environment, so the sign-in and request shapes follow RAM's published Swagger page and
what was observed from outside (a bad client answers HTTP 401 {"error": "Unauthorized"}; a wrong API user answers HTTP 400
{"error": "invalid_grant"}) and are exactly as good as those descriptions.
"""

from __future__ import annotations

import datetime as dt
import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.integrations.ramtracking import RamError, RamTracking, missing_credentials, origin_of
from jarvis.main import create_app
from jarvis.services import connection_tests
from jarvis.services.tracking import Tracker
from jarvis.settings_store import SettingsStore
from tests.fakes import FakeClient

SECRET, PASSWORD, USERNAME = "client-secret-XYZ", "p+a&s%s=#x y", "MAC"
# Shaped exactly like RAM's published VehicleDTO (https://api.qaifn.co.uk/v2/api-docs): vehicle_status.last_event is a
# plain STRING, not an object. (Reading it as an object was the real cause of the live failure "'str' object has no
# attribute 'get'", and the tests missed it because they built the response by hand with an object there.)
VEHICLE = {"id": 101, "registration": "YD71 SFS", "alias": "Dan's van", "vehicle_type": "VAN", "odometer": 41230.5,
           "vehicle_driver": {"id": 7, "name": "Dan Harper", "mobileNumber": "07700900123"},
           "vehicle_status": {"event_date": "2026-10-02T09:00:00Z", "last_event": "TRANSIT_START",
                              "location": {"latitude": 53.83, "longitude": -1.78},
                              "rawLocation": {"latitude": 53.8301, "longitude": -1.7799}, "sufficientGpsAccuracy": True}}
PARKED = {**VEHICLE, "id": 102, "registration": "YD72 SFS", "vehicle_driver": {"name": "Priya Shah"},
          "vehicle_status": {**VEHICLE["vehicle_status"], "last_event": "IGNITION_OFF"}}
# LocationHistoryDTO items, as published
HISTORY = {"id": 101, "registration": "YD71 SFS", "history": [
    {"event_date": "2026-10-01T07:05:00Z", "event_name": "TRANSIT_START", "latitude": 53.80, "longitude": -1.80,
     "formattedAddress": "Home", "postCode": "BD17 7AA", "speedKph": 0, "odometer": 41000.0, "heading": "N"},
    {"event_date": "2026-10-01T07:25:00Z", "event_name": "TRANSIT_STOP", "latitude": 53.83, "longitude": -1.78,
     "formattedAddress": "Saltaire Court Offices", "postCode": "BD18 3LA", "speedKph": 0, "odometer": 41012.4},
]}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Ram:
    """A scripted RAM. ``token`` / ``vehicles`` / ``history`` are handlers (request -> Response); every request is logged."""

    def __init__(self, token=None, vehicles=None, history=None):
        self.log: list[httpx.Request] = []
        self.token = token or (lambda r: httpx.Response(200, json={"access_token": "tok", "expires_in": 28799}))
        self.vehicles = vehicles or (lambda r: httpx.Response(200, json=[VEHICLE]))
        self.history = history or (lambda r: httpx.Response(200, json={"history": []}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.log.append(request)
        path = request.url.path
        if path == "/oauth/token":
            return self.token(request)
        if path.startswith("/api/v1/history/"):
            return self.history(request)
        if path == "/api/v1/vehicle/for-account":
            return self.vehicles(request)
        return httpx.Response(404, json={"error": "Not Found"})

    def count(self, path_part: str) -> int:
        return sum(1 for r in self.log if path_part in r.url.path)


def configure(settings, **over):
    values = dict(ram_client_id="Alex Clancy", ram_api_key=SECRET, ram_username=USERNAME, ram_password=PASSWORD)
    for key, value in {**values, **over}.items():
        setattr(settings, key, value)
    return settings


def jarvis(settings, ram: Ram, **over) -> Jarvis:
    configure(settings, **over)
    http = httpx.AsyncClient(transport=httpx.MockTransport(ram))
    j = Jarvis(settings, client=FakeClient(), http=http)
    assert isinstance(j.ram, RamTracking)
    j.ram._now = Clock()
    return j


# --------------------------------------------------------------------------- "credentials not detected"
def test_missing_details_are_named_one_by_one_and_the_app_stays_on_demo_until_all_four_are_in(settings):
    assert missing_credentials(settings) == ["Client ID", "Client secret", "API username", "API password"]
    configure(settings, ram_username="", ram_password="  ")  # whitespace is not a password
    assert missing_credentials(settings) == ["API username", "API password"]
    j = Jarvis(settings, client=FakeClient())
    assert j.ram.demo
    status = j.connections()["Vehicle tracking"]
    assert status.startswith("DEMO journeys - still missing: API username, API password")


async def test_the_connection_test_says_exactly_which_detail_is_missing(settings):
    configure(settings, ram_username="")
    j = Jarvis(settings, client=FakeClient())
    ok, detail = await connection_tests.run(j, "ram")
    assert ok is False and "API username" in detail and "Client ID" not in detail.split("All four")[0]
    await j.http.aclose()


# --------------------------------------------------------------------------- the 404: a pasted endpoint as the API address
@pytest.mark.parametrize("pasted", [
    "https://api.qaifn.co.uk/api/v1/vehicle/for-account",  # the full endpoint (what was found on the live Connections form)
    "https://api.qaifn.co.uk/swagger/docs",
    "https://api.qaifn.co.uk/api/v1/",
    "https://api.qaifn.co.uk/",
])
async def test_an_api_address_with_a_path_on_it_no_longer_404s(settings, pasted):
    ram = Ram()
    j = jarvis(settings, ram, ram_api_base_url=pasted)
    assert origin_of(pasted) == "https://api.qaifn.co.uk"
    assert [v["registration"] for v in await j.ram.vehicles()] == ["YD71 SFS"]
    assert ram.log[-1].url.path == "/api/v1/vehicle/for-account"  # not .../for-account/api/v1/vehicle/for-account
    await j.http.aclose()


async def test_the_test_result_mentions_that_a_path_was_ignored(settings):
    j = jarvis(settings, Ram(), ram_api_base_url="https://api.qaifn.co.uk/api/v1/vehicle/for-account")
    ok, detail = await connection_tests.run(j, "ram")
    assert ok and "1 vehicles" in detail and "only https://api.qaifn.co.uk is used" in detail
    await j.http.aclose()


def test_saving_an_api_address_keeps_only_the_host(settings):
    store = SettingsStore(settings)
    assert store.validate("ram_api_base_url", "https://api.qaifn.co.uk/api/v1/vehicle/for-account")[0] == \
        "https://api.qaifn.co.uk"
    assert store.validate("ram_api_base_url", "api.qaifn.co.uk/swagger")[0] == "https://api.qaifn.co.uk"


async def test_a_404_on_the_vehicle_call_blames_the_address_when_it_is_not_rams_own(settings):
    ram = Ram(vehicles=lambda r: httpx.Response(404, json={"error": "Not Found"}))
    j = jarvis(settings, ram, ram_api_base_url="https://rtapi.example.test")
    with pytest.raises(RamError) as e:
        await j.ram.vehicles()
    text = str(e.value)
    assert "HTTP 404" in text and "rtapi.example.test" in text and "not RAM's standard https://api.qaifn.co.uk" in text
    assert e.value.stage == "data"
    await j.http.aclose()


async def test_a_404_with_the_standard_address_says_sign_in_worked_so_the_path_may_have_changed(settings):
    ram = Ram(vehicles=lambda r: httpx.Response(404, json={}))
    j = jarvis(settings, ram)
    ok, detail = await connection_tests.run(j, "ram")
    assert ok is False and "Sign-in worked" in detail and "ram_endpoints.yaml" in detail
    await j.http.aclose()


# --------------------------------------------------------------------------- the sign-in (the token request)
def _secrets_absent(text: str) -> None:
    for secret in (SECRET, PASSWORD, USERNAME):
        assert secret not in text, secret


async def test_a_bad_client_is_a_401_and_the_message_says_so(settings):
    ram = Ram(token=lambda r: httpx.Response(401, json={"error": "Unauthorized", "message": "Bad credentials"}))
    j = jarvis(settings, ram)
    ok, detail = await connection_tests.run(j, "ram")
    assert ok is False and "HTTP 401" in detail and "Bad credentials" in detail
    assert "Client ID and Client secret" in detail and "not what is wrong" in detail
    _secrets_absent(detail)
    assert j.ram.health["ok"] is False
    assert j.vehicle_tracking_status().startswith("NOT CONNECTED - RAM Tracking is failing: RAM refused the sign-in")
    await j.http.aclose()


async def test_a_wrong_api_user_is_a_400_invalid_grant_and_the_message_says_so(settings):
    ram = Ram(token=lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad credentials"}))
    j = jarvis(settings, ram)
    ok, detail = await connection_tests.run(j, "ram")
    assert ok is False and "HTTP 400" in detail and "invalid_grant" in detail and "Bad credentials" in detail
    assert "accepted the Client ID and Client secret but refused the API user's username or password" in detail
    assert "RAM username" in detail and "email" not in detail  # a short RAM username like "MAC" is not an error
    _secrets_absent(detail)
    await j.http.aclose()


@pytest.mark.parametrize("code,needle", [("invalid_scope", "scope"), ("unsupported_grant_type", "doesn't accept a username")])
async def test_other_oauth_errors_are_named(settings, code, needle):
    ram = Ram(token=lambda r: httpx.Response(400, json={"error": code, "error_description": "nope"}))
    j = jarvis(settings, ram)
    with pytest.raises(RamError) as e:
        await j.ram.vehicles()
    assert code in str(e.value) and needle in str(e.value)
    await j.http.aclose()


async def test_an_error_body_that_echoes_an_email_or_runs_long_is_cleaned_before_it_is_shown(settings):
    body = {"error": "invalid_grant", "error_description": "No account for someone@example.com " + "z" * 400}
    j = jarvis(settings, Ram(token=lambda r: httpx.Response(400, json=body)))
    with pytest.raises(RamError) as e:
        await j.ram.vehicles()
    assert "someone@example.com" not in str(e.value) and "[email]" in str(e.value) and "z" * 200 not in str(e.value)
    await j.http.aclose()


async def test_a_token_address_that_is_not_found_is_called_out(settings):
    j = jarvis(settings, Ram(token=lambda r: httpx.Response(404, json={})))
    ok, detail = await connection_tests.run(j, "ram")
    assert ok is False and "sign-in address wasn't found" in detail and "auth.qaifn.co.uk/oauth/token" in detail
    await j.http.aclose()


async def test_the_form_body_carries_odd_characters_exactly_and_stray_whitespace_is_stripped(settings):
    ram = Ram()
    j = jarvis(settings, ram, ram_username=f" {USERNAME}\n", ram_password=f"{PASSWORD}\r\n", ram_client_id="Alex Clancy ")
    await j.ram.vehicles()
    request = ram.log[0]
    form = parse_qs(request.content.decode())
    assert form == {"grant_type": ["password"], "username": [USERNAME], "password": [PASSWORD]}
    import base64

    assert request.headers["authorization"] == "Basic " + base64.b64encode(f"Alex Clancy:{SECRET}".encode()).decode()
    await j.http.aclose()


# --------------------------------------------------------------------------- failing is shown honestly, never as a blank map
async def test_the_fleet_data_and_the_connection_line_say_why_when_ram_is_failing(settings, monkeypatch):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    ram = Ram(vehicles=lambda r: httpx.Response(404, json={}))
    j = jarvis(settings, ram, ram_api_base_url="https://rtapi.example.test")
    live = await j.tracker.live()
    assert live["engineers"] == [] and "HTTP 404" in live["ram_error"] and "rtapi.example.test" in live["ram_error"]
    status = j.connections()["Vehicle tracking"]
    assert status.startswith("NOT CONNECTED") and "DEMO" not in status  # failing, not "sample data"
    await j.http.aclose()


async def test_the_api_returns_the_reason_not_a_500_and_status_probes_ram(settings, monkeypatch):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    ram = Ram(token=lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad credentials"}))
    j = jarvis(settings, ram)
    with TestClient(create_app(settings, j)) as c:
        assert "NOT CONNECTED" in c.get("/api/status").json()["connections"]["Vehicle tracking"]  # the probe ran
        tracking = c.get("/api/tracking")
        assert tracking.status_code == 200 and "invalid_grant" in tracking.json()["ram_error"]
    await j.http.aclose()


async def test_a_working_connection_reads_as_connected_and_shows_vehicles(settings, monkeypatch):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    j = jarvis(settings, Ram())
    with TestClient(create_app(settings, j)) as c:
        assert c.get("/api/status").json()["connections"]["Vehicle tracking"] == "RAM Tracking"
        live = c.get("/api/tracking").json()
        assert [e["vehicle"] for e in live["engineers"]] == ["YD71 SFS"] and "ram_error" not in live
    await j.http.aclose()


# --------------------------------------------------------------------------- RAM's limit: 3 requests a minute per endpoint
async def test_everything_that_wants_vehicles_shares_one_request_a_minute(settings):
    ram = Ram()
    j = jarvis(settings, ram)
    for _ in range(5):  # the map, a chat tool, the routine test, the connection test, the health probe
        await j.ram.positions()
        await j.ram.vehicles()
        await j.ram.check()
        await j.ram.probe(max_age=0)
    assert ram.count("/oauth/token") == 1 and ram.count("/vehicle/for-account") == 1  # one token, one vehicle list
    j.ram._now.t += 61  # a minute later it asks again, with the same token
    await j.ram.vehicles()
    assert ram.count("/vehicle/for-account") == 2 and ram.count("/oauth/token") == 1
    await j.http.aclose()


async def test_journeys_are_cached_and_never_asked_for_faster_than_three_a_minute(settings):
    ram = Ram()
    j = jarvis(settings, ram)
    day = dt.date.today() - dt.timedelta(days=1)
    for _ in range(3):
        await j.ram.journeys("101", day)
    assert ram.count("/history/") == 1  # a finished day is fetched once
    await j.ram.journeys("102", day)
    await j.ram.journeys("103", day)
    assert ram.count("/history/") == 3
    with pytest.raises(RamError) as e:  # a fourth different request inside the minute is not sent at all
        await j.ram.journeys("104", day)
    assert e.value.rate_limited and ram.count("/history/") == 3 and "rate limited" in str(e.value)
    assert j.ram.health["ok"] is not False  # busy is not broken
    j.ram._now.t += 61
    await j.ram.journeys("104", day)
    assert ram.count("/history/") == 4
    await j.http.aclose()


async def test_todays_journeys_are_refreshed_after_two_minutes_but_not_before(settings):
    ram = Ram()
    j = jarvis(settings, ram)
    today = dt.date.today()
    await j.ram.journeys("101", today)
    j.ram._now.t += 90
    await j.ram.journeys("101", today)
    assert ram.count("/history/") == 1
    j.ram._now.t += 40
    await j.ram.journeys("101", today)
    assert ram.count("/history/") == 2
    await j.http.aclose()


async def test_an_http_429_is_rate_limited_not_not_connected_and_the_last_answer_is_still_served(settings, monkeypatch):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    state = {"limited": False}

    def vehicles(request):
        return (httpx.Response(429, headers={"Retry-After": "30"}, json={"error": "Too Many Requests"})
                if state["limited"] else httpx.Response(200, json=[VEHICLE]))

    ram = Ram(vehicles=vehicles)
    j = jarvis(settings, ram)
    assert len(await j.ram.vehicles()) == 1
    state["limited"] = True
    j.ram._now.t += 61  # cache expired, RAM now says 429
    assert len(await j.ram.vehicles()) == 1  # the previous answer is served instead of an error
    assert j.ram.health["rate_limited"] is True and j.ram.health["ok"] is True
    assert j.vehicle_tracking_status() == "RAM Tracking (rate limited just now, retry shortly)"
    sent = ram.count("/vehicle/for-account")
    await j.ram.vehicles()
    assert ram.count("/vehicle/for-account") == sent  # Retry-After is honoured: nothing is sent while blocked
    j.ram._now.t += 31
    state["limited"] = False
    await j.ram.vehicles()
    assert j.ram.health["rate_limited"] is False and j.vehicle_tracking_status() == "RAM Tracking"
    await j.http.aclose()


async def test_a_429_with_nothing_cached_says_rate_limited_in_the_test_and_the_map_and_is_not_a_failure(settings, monkeypatch):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    ram = Ram(vehicles=lambda r: httpx.Response(429, json={}))
    j = jarvis(settings, ram)
    ok, detail = await connection_tests.run(j, "ram")
    assert ok is True and "Signed in to RAM" in detail and "rate limited" in detail and "not a connection fault" in detail
    live = await j.tracker.live()
    assert live["rate_limited"] is True and "ram_error" not in live and live["engineers"] == []
    assert not j.vehicle_tracking_status().startswith("NOT CONNECTED")
    await j.http.aclose()


async def test_a_token_that_is_refused_mid_life_is_replaced_once_and_the_request_retried(settings):
    calls = {"n": 0}

    def vehicles(request):
        calls["n"] += 1
        return httpx.Response(401, json={}) if calls["n"] == 1 else httpx.Response(200, json=[VEHICLE])

    ram = Ram(vehicles=vehicles)
    j = jarvis(settings, ram)
    assert len(await j.ram.vehicles()) == 1
    assert ram.count("/oauth/token") == 2 and ram.count("/vehicle/for-account") == 2
    await j.http.aclose()


async def test_a_refused_request_after_a_good_sign_in_is_a_clear_permissions_message(settings):
    j = jarvis(settings, Ram(vehicles=lambda r: httpx.Response(403, json={})))
    with pytest.raises(RamError) as e:
        await j.ram.vehicles()
    assert "HTTP 403" in str(e.value) and "may not have access to vehicles" in str(e.value)
    await j.http.aclose()


async def test_nothing_secret_is_in_any_message_or_log_line(settings, caplog):
    ram = Ram(token=lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad credentials"}))
    j = jarvis(settings, ram)
    with caplog.at_level("DEBUG"):
        with pytest.raises(RamError):
            await j.ram.vehicles()
    blob = json.dumps([r.getMessage() for r in caplog.records]) + j.connections()["Vehicle tracking"]
    _secrets_absent(blob)
    await j.http.aclose()
