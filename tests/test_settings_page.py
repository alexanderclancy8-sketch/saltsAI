"""The Settings page: saving connections, secret handling, live reload, and the Test buttons."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.settings_store import SettingsStore
from tests.fakes import FakeClient, message, text_block


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


def test_view_lists_sections_and_masks_secrets(settings):
    settings.fsm_api_key = "topsecret12345"
    store = SettingsStore(settings)
    view = store.view(type("Db", (), {"get_kv": staticmethod(lambda k: None)}), {"base_url": "https://x", "app_name": "x"})
    fsm = next(s for s in view["sections"] if s["id"] == "fsm")
    key_field = next(f for f in fsm["fields"] if f["key"] == "fsm_api_key")
    assert key_field["is_set"] is True and "2345" in key_field["hint"] and "topsecret" not in json.dumps(key_field)
    assert fsm["configured"] is False  # fsm_base_url still blank


def test_validation_rejects_bad_values_and_saves_nothing(settings):
    store = SettingsStore(settings)
    errors = store.update({"owner_email": "not-an-email", "fsm_base_url": "not-a-url"}, [])
    assert "owner_email" in errors and "fsm_base_url" in errors
    assert store.overrides == {} and settings.owner_email == ""


def test_save_applies_immediately_and_persists_encrypted(settings):
    store = SettingsStore(settings)
    errors = store.update({"owner_email": "alex@example.com", "fsm_base_url": "https://fsm.example.com"}, [])
    assert errors == {}
    assert settings.owner_email == "alex@example.com" and settings.fsm_base_url == "https://fsm.example.com"
    raw = store.path.read_bytes()
    assert b"alex@example.com" not in raw  # encrypted on disk

    reloaded = SettingsStore(settings)
    assert reloaded.overrides["owner_email"] == "alex@example.com"


def test_blank_secret_leaves_existing_value_untouched(settings):
    store = SettingsStore(settings)
    store.update({"fsm_api_key": "realkey123"}, [])
    store.update({"fsm_api_key": "", "owner_name": "Alex"}, [])
    assert settings.fsm_api_key == "realkey123" and settings.owner_name == "Alex"


def test_clear_removes_an_override_back_to_the_azure_value(settings, monkeypatch):
    monkeypatch.setenv("OWNER_NAME", "FromAzure")
    store = SettingsStore(settings)
    store.update({"owner_name": "Overridden"}, [])
    assert settings.owner_name == "Overridden"
    store.update({}, ["owner_name"])
    assert "owner_name" not in store.overrides


def test_a_setting_equal_to_the_base_value_is_not_kept_as_an_override(settings):
    store = SettingsStore(settings)
    store.update({"owner_name": settings.owner_name}, [])
    assert "owner_name" not in store.overrides


def test_unreadable_store_starts_fresh_instead_of_crashing(settings):
    store = SettingsStore(settings)
    store.update({"owner_name": "Alex"}, [])
    store.path.write_bytes(b"not encrypted data")
    fresh = SettingsStore(settings)
    assert fresh.overrides == {} and "again" in fresh.problem


def test_cron_and_password_and_token_are_checked(settings):
    store = SettingsStore(settings)
    errors = store.update({"briefing_cron": "not a cron", "jarvis_owner_password": "short",
                           "claude_code_oauth_token": "wrong-prefix"}, [])
    assert set(errors) == {"briefing_cron", "jarvis_owner_password", "claude_code_oauth_token"}


def test_settings_api_requires_login_and_saves(settings):
    settings.jarvis_owner_password = "s3cret"
    app = create_app(settings, make(settings))
    with TestClient(app) as c:
        assert c.get("/api/settings").status_code == 401
        c.post("/login", data={"password": "s3cret"})
        r = c.get("/api/settings")
        assert r.status_code == 200
        sections = {s["id"] for s in r.json()["sections"]}
        assert {"profile", "claude", "microsoft365", "fsm", "sage", "voice", "security"} <= sections

        bad = c.post("/api/settings", json={"values": {"owner_email": "nope"}, "clear": []})
        assert bad.status_code == 400 and "owner_email" in bad.json()["errors"]

        ok = c.post("/api/settings", json={"values": {"owner_name": "Sam"}, "clear": []})
        assert ok.status_code == 200 and ok.json()["context"]["backend"] in ("api", "max")


def test_saving_a_setting_reloads_jarvis_keeping_the_conversation(settings):
    settings.jarvis_owner_password = "s3cret"
    j = make(settings, [message([text_block("Cash is fine, sir.")])])
    app = create_app(settings, j)
    with TestClient(app) as c:
        c.post("/login", data={"password": "s3cret"})
        r = c.post("/api/chat", json={"text": "How's cash?"})
        assert r.json()["reply"] == "Cash is fine, sir."
        first_jarvis = app.state.j
        assert len(first_jarvis.brain.messages) == 2

        c.post("/api/settings", json={"values": {"owner_name": "Sam"}, "clear": []})
        assert app.state.j is not first_jarvis  # a new instance, built from the new settings
        assert app.state.j.settings.owner_name == "Sam"
        assert len(app.state.j.brain.messages) == 2  # the conversation carried over


def test_changing_the_password_signs_everyone_out(settings):
    settings.jarvis_owner_password = "s3cret"
    app = create_app(settings, make(settings))
    with TestClient(app) as c:
        c.post("/login", data={"password": "s3cret"})
        r = c.post("/api/settings", json={"values": {"jarvis_owner_password": "newpassword1"}, "clear": []})
        assert r.status_code == 200 and r.json()["signed_out"] is True
        assert c.get("/api/status").status_code == 401
        c.post("/login", data={"password": "newpassword1"})
        assert c.get("/api/status").status_code == 200


async def test_connection_test_claude_asks_the_real_backend(settings):
    settings.jarvis_owner_password = "s3cret"
    j = make(settings, [message([text_block("OK")])])
    app = create_app(settings, j)
    with TestClient(app) as c:
        c.post("/login", data={"password": "s3cret"})
        r = c.post("/api/settings/test/claude")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert c.post("/api/settings/test/profile").status_code == 404  # not a testable section


def test_connection_test_reports_when_nothing_is_configured(settings):
    settings.jarvis_owner_password = "s3cret"
    app = create_app(settings, make(settings))
    with TestClient(app) as c:
        c.post("/login", data={"password": "s3cret"})
        r = c.post("/api/settings/test/fsm")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and "first" in body["detail"].lower()


def test_test_result_is_marked_stale_after_the_settings_change(settings):
    settings.jarvis_owner_password = "s3cret"
    app = create_app(settings, make(settings))
    with TestClient(app) as c:
        c.post("/login", data={"password": "s3cret"})
        c.post("/api/settings/test/fsm")
        before = next(s for s in c.get("/api/settings").json()["sections"] if s["id"] == "fsm")
        assert before["last_test"] is not None and not before["last_test"].get("stale")

        c.post("/api/settings", json={"values": {"fsm_base_url": "https://fsm.example.com"}, "clear": []})
        after = next(s for s in c.get("/api/settings").json()["sections"] if s["id"] == "fsm")
        assert after["last_test"]["stale"] is True
