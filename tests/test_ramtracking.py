"""RAM Tracking's real OAuth2 API: token fetch/caching, and parsing RAM's actual (nested)
response shapes for vehicles/positions/journeys, confirmed against their Swagger docs
(https://api.qaifn.co.uk/swagger/docs). See jarvis/integrations/ramtracking.py's docstring."""

import json

import httpx

from jarvis.config import Settings
from jarvis.integrations.ramtracking import RamTracking

SETTINGS = dict(ram_client_id="Alex Clancy", ram_api_key="secret-123", ram_username="api-user",
                ram_password="api-pass", _env_file=None)


def _vehicle(id_, reg, driver, lat, lng, event, event_date="2026-10-01T09:00:00Z"):
    return {
        "id": id_, "registration": reg,
        "vehicle_driver": {"name": driver},
        "vehicle_status": {"event_date": event_date, "location": {"latitude": lat, "longitude": lng},
                            "last_event": {"event": event}},
    }


async def test_access_token_is_fetched_with_basic_auth_and_cached():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            calls.append({"auth": request.headers.get("authorization"), "body": request.content.decode()})
            return httpx.Response(200, json={"access_token": "tok-1", "token_type": "bearer", "expires_in": 28799})
        return httpx.Response(200, json=[])

    s = Settings(**SETTINGS)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ram = RamTracking(s, http)
        tok1 = await ram._access_token()
        tok2 = await ram._access_token()  # cached - no second POST
    assert tok1 == tok2 == "tok-1"
    assert len(calls) == 1
    import base64
    assert calls[0]["auth"] == "Basic " + base64.b64encode(b"Alex Clancy:secret-123").decode()
    assert "grant_type=password" in calls[0]["body"] and "username=api-user" in calls[0]["body"]
    assert "password=api-pass" in calls[0]["body"]


async def test_vehicles_parses_rams_nested_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 28799})
        assert request.url.path == "/api/v1/vehicle/for-account"
        assert request.headers.get("authorization") == "bearer tok"
        return httpx.Response(200, json=[
            _vehicle(101, "YD71 SFS", "Dan Harper", 53.83, -1.78, "TRANSIT_START"),
            _vehicle(102, "YD72 SFS", "Priya Shah", 53.80, -1.75, "IDLE_START"),
        ])

    s = Settings(**SETTINGS)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        vehicles = await RamTracking(s, http).vehicles()
    assert vehicles[0] == {"id": 101, "registration": "YD71 SFS", "driver": "Dan Harper", "lat": 53.83,
                           "lng": -1.78, "timestamp": "2026-10-01T09:00:00Z", "moving": True}
    assert vehicles[1]["driver"] == "Priya Shah" and vehicles[1]["moving"] is False


async def test_positions_reshapes_vehicles_since_ram_has_no_bulk_positions_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 28799})
        return httpx.Response(200, json=[
            _vehicle(101, "YD71 SFS", "Dan Harper", 53.83, -1.78, "TRANSIT_START"),
            _vehicle(102, "YD72 SFS", "Priya Shah", None, None, "IDLE_START"),  # no fix yet - dropped
        ])

    s = Settings(**SETTINGS)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        positions = await RamTracking(s, http).positions()
    assert len(positions) == 1
    assert positions[0]["vehicle_id"] == 101 and positions[0]["speed_mph"] == 15


async def test_journeys_groups_raw_events_into_legs_between_transit_start_and_stop():
    seen_path = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 28799})
        seen_path["path"] = request.url.path
        return httpx.Response(200, json={"id": 101, "registration": "YD71 SFS", "history": [
            {"event_date": "2026-10-01T07:00:00", "event_name": "IGNITION_ON", "latitude": 53.80, "longitude": -1.80},
            {"event_date": "2026-10-01T07:05:00", "event_name": "TRANSIT_START", "latitude": 53.80, "longitude": -1.80,
             "formattedAddress": "Home"},
            {"event_date": "2026-10-01T07:25:00", "event_name": "TRANSIT_STOP", "latitude": 53.83, "longitude": -1.78,
             "formattedAddress": "Saltaire Court Offices"},
            {"event_date": "2026-10-01T09:00:00", "event_name": "TRANSIT_START", "latitude": 53.83, "longitude": -1.78,
             "formattedAddress": "Saltaire Court Offices"},
            {"event_date": "2026-10-01T09:15:00", "event_name": "TRANSIT_STOP", "latitude": 53.85, "longitude": -1.76,
             "formattedAddress": "Otley Road Hotel"},
        ]})

    s = Settings(**SETTINGS)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        import datetime
        legs = await RamTracking(s, http).journeys("101", datetime.date(2026, 10, 1))
    assert "/api/v1/history/101/" in seen_path["path"]
    assert len(legs) == 2
    assert legs[0]["start_address"] == "Home" and legs[0]["end_address"] == "Saltaire Court Offices"
    assert legs[0]["start_time"] == "2026-10-01T07:05:00" and legs[0]["end_time"] == "2026-10-01T07:25:00"
    assert legs[1]["end_address"] == "Otley Road Hotel"
    # No reliable distance unit documented by RAM for the history odometer field - never guess one.
    assert legs[0]["distance_miles"] is None
