"""Competitor comparison: Google rating/reviews plus the same SEO snapshot run on both our site and theirs."""

from __future__ import annotations

import json

import httpx
import pytest

from jarvis.brain.tools import CompetitorAuditIn, TOOLS_BY_NAME
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.db import Database
from jarvis.integrations.marketing import PresenceSources
from jarvis.services.marketing import MarketingTracker
from tests.fakes import FakeClient

MINIMAL_HOME_PAGE = (
    "<html><head><title>Fire and security in Bradford, Leeds and Halifax</title>"
    "<meta name='description' content='BAFE and SSAIB accredited fire alarm, CCTV and intruder alarm "
    "installation and servicing across West Yorkshire, Bradford, Leeds and Halifax.'></head>"
    "<body><h1>Fire and security</h1><p>Fire alarm, CCTV, intruder alarm, access control, emergency lighting, "
    "extinguisher servicing and installation across Bradford, Leeds, Halifax, Shipley and Baildon. BAFE and "
    "SSAIB accredited.</p></body></html>"
)


@pytest.fixture
def http_and_seen():
    seen = {"places_requests": [], "urls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen["urls"].append(url)
        if url.endswith("/places:searchText"):
            seen["places_requests"].append(json.loads(request.content))
            return httpx.Response(200, json={"places": [{
                "displayName": {"text": "Firetech Solutions Ltd"}, "rating": 4.2, "userRatingCount": 18,
                "websiteUri": "https://firetech.example.co.uk", "formattedAddress": "12 High St, Bradford"}]})
        if "places.googleapis.com" in url and "/places/" in url:
            return httpx.Response(200, json={"displayName": {"text": "Salts"}, "rating": 4.8, "userRatingCount": 132})
        if "saltsfireandsecurity" in url or "firetech.example" in url:
            return httpx.Response(200, text=MINIMAL_HOME_PAGE)
        return httpx.Response(404)  # robots.txt / sitemap.xml / pagespeed - seo_audit already tolerates this

    return handler, seen


async def test_find_business_returns_the_top_match(http_and_seen):
    handler, seen = http_and_seen
    s = Settings(google_places_api_key="places-key", google_place_id="place-1", _env_file=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await PresenceSources(s, http).find_business("Firetech Solutions Bradford")
    assert result == {"name": "Firetech Solutions Ltd", "rating": 4.2, "reviews": 18,
                      "website": "https://firetech.example.co.uk", "address": "12 High St, Bradford"}
    assert seen["places_requests"][0] == {"textQuery": "Firetech Solutions Bradford"}


async def test_find_business_reports_no_match():
    s = Settings(google_places_api_key="places-key", google_place_id="place-1", _env_file=None)

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"places": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(empty)) as http:
        result = await PresenceSources(s, http).find_business("Nobody Ltd")
    assert "error" in result and "Nobody Ltd" in result["error"]


async def test_competitor_audit_compares_us_and_named_competitors(tmp_path, http_and_seen):
    handler, seen = http_and_seen
    s = Settings(website_url="https://www.saltsfireandsecurity.co.uk", google_places_api_key="places-key",
                google_place_id="place-1", _env_file=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        presence = PresenceSources(s, http)
        marketing = MarketingTracker(s, Database(tmp_path / "db.sqlite"), http, presence, None, None)
        result = await marketing.competitor_audit(["Firetech Solutions Bradford"])

    assert result["us"]["name"] == "Salts Fire and Security" and result["us"]["rating"] == 4.8
    assert result["us"]["seo"]["title"].startswith("Fire and security")

    competitor = result["competitors"][0]
    assert competitor["name"] == "Firetech Solutions Ltd" and competitor["rating"] == 4.2
    assert competitor["website"] == "https://firetech.example.co.uk"
    assert competitor["seo"]["title"].startswith("Fire and security")  # same audit ran against their site too


async def test_competitor_audit_without_a_places_key_still_runs_our_own_seo(tmp_path, http_and_seen):
    handler, _ = http_and_seen
    s = Settings(website_url="https://www.saltsfireandsecurity.co.uk", _env_file=None)  # no Google Places key
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        presence = PresenceSources(s, http)
        marketing = MarketingTracker(s, Database(tmp_path / "db.sqlite"), http, presence, None, None)
        result = await marketing.competitor_audit(["Firetech Solutions Bradford"])

    assert "rating" not in result["us"] and "seo" in result["us"]  # our own SEO snapshot still runs
    competitor = result["competitors"][0]
    assert "note" in competitor and "website" not in competitor  # no way to find their site without Places
    assert "seo" not in competitor


async def test_competitor_audit_looks_up_competitors_with_just_a_places_key_no_place_id_yet(tmp_path, http_and_seen):
    # google_reviews() (our own listing) needs both the key and our place ID; find_business() (a competitor,
    # looked up by name) only ever needs the key. An owner who has only added the key so far must still get
    # competitor ratings - the "us" side is the only part that should stay blank.
    handler, _ = http_and_seen
    s = Settings(website_url="https://www.saltsfireandsecurity.co.uk", google_places_api_key="places-key",
                _env_file=None)  # no google_place_id yet
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        presence = PresenceSources(s, http)
        marketing = MarketingTracker(s, Database(tmp_path / "db.sqlite"), http, presence, None, None)
        result = await marketing.competitor_audit(["Firetech Solutions Bradford"])

    assert "rating" not in result["us"]  # our own listing needs a place ID we don't have yet
    competitor = result["competitors"][0]
    assert "note" not in competitor
    assert competitor["name"] == "Firetech Solutions Ltd" and competitor["rating"] == 4.2


async def test_competitor_audit_caps_at_six_and_keeps_going_after_one_lookup_fails(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/places:searchText"):
            body = json.loads(request.content)
            if "Broken" in body["textQuery"]:
                return httpx.Response(500)
            return httpx.Response(200, json={"places": [{"displayName": {"text": body["textQuery"]}, "rating": 4.0,
                                                          "userRatingCount": 5}]})
        if "places.googleapis.com" in url:
            return httpx.Response(200, json={"rating": 4.8, "userRatingCount": 132})
        return httpx.Response(200, text=MINIMAL_HOME_PAGE)

    s = Settings(website_url="https://www.saltsfireandsecurity.co.uk", google_places_api_key="k",
                google_place_id="p", _env_file=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        presence = PresenceSources(s, http)
        marketing = MarketingTracker(s, Database(tmp_path / "db.sqlite"), http, presence, None, None)
        result = await marketing.competitor_audit(["Broken One", "Good Two", "Good Three", "Good Four",
                                                    "Good Five", "Good Six", "Good Seven - dropped"])

    assert len(result["competitors"]) == 6  # the 7th name is never even looked up
    assert "error" in result["competitors"][0]  # the failing lookup is reported, not raised
    assert result["competitors"][1]["name"] == "Good Two"


# --------------------------------------------------------------------------- the chat tool
async def test_competitor_audit_tool_is_read_only_and_dispatches_to_marketing(settings, monkeypatch):
    j = Jarvis(settings, client=FakeClient())
    tool = TOOLS_BY_NAME["competitor_audit"]
    assert not tool.approval  # research only - nothing to approve

    seen = {}

    async def fake_competitor_audit(competitors):
        seen["competitors"] = competitors
        return {"us": {}, "competitors": []}

    monkeypatch.setattr(j.marketing, "competitor_audit", fake_competitor_audit)
    result = await tool.handler(j, CompetitorAuditIn(competitors=["Firetech Solutions Bradford"]))
    assert seen["competitors"] == ["Firetech Solutions Bradford"] and result == {"us": {}, "competitors": []}
    await j.http.aclose()
