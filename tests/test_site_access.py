"""Engineer/access codes: recording one always goes through the approval queue like any other write, a
lookup never invents or searches for one, and codes are actually encrypted at rest - not just labelled as
such."""

from __future__ import annotations

import asyncio

from jarvis.brain.tools import SiteAccessCodeIn, SiteAccessCodeUpdateIn, TOOLS_BY_NAME, dispatch, site_access_code
from jarvis.core import Jarvis
from tests.fakes import FakeClient


def make(settings):
    return Jarvis(settings, client=FakeClient())


async def test_recording_a_code_queues_for_approval_not_written_immediately(settings):
    j = make(settings)
    tool = TOOLS_BY_NAME["site_access_code_update"]
    assert tool.approval is True
    result = await dispatch(j, tool, SiteAccessCodeUpdateIn(site="Kestrel Industrial Estate",
                                                            system="Fire alarm panel - Kentec Syncro",
                                                            code="1234", notes="Set during commissioning"))
    assert "queued" in result.lower()
    assert j.site_access.find("Kestrel") == []  # not recorded until approved
    await j.http.aclose()


async def test_approving_records_it_encrypted_and_lookup_decrypts_it(settings):
    j = make(settings)
    tool = TOOLS_BY_NAME["site_access_code_update"]
    result = await dispatch(j, tool, SiteAccessCodeUpdateIn(site="Kestrel Industrial Estate",
                                                            system="Fire alarm panel - Kentec Syncro",
                                                            code="1234", notes=""))
    pending = j.db.pending_actions()
    assert len(pending) == 1
    await j.actions.approve(pending[0]["id"])
    await asyncio.sleep(0.05)
    assert j.db.get_action(pending[0]["id"])["status"] == "done"

    raw = j.db.find_site_access_codes("Kestrel")
    assert len(raw) == 1 and raw[0]["code_encrypted"] != "1234" and "1234" not in raw[0]["code_encrypted"]

    found = await site_access_code(j, SiteAccessCodeIn(site="Kestrel"))
    assert found["matches"][0]["code"] == "1234"
    assert found["matches"][0]["system"] == "Fire alarm panel - Kentec Syncro"
    await j.http.aclose()


async def test_lookup_for_an_unknown_site_returns_nothing_not_a_guess(settings):
    j = make(settings)
    found = await site_access_code(j, SiteAccessCodeIn(site="Somewhere Salts has never worked"))
    assert found["matches"] == []
    await j.http.aclose()


async def test_a_changed_secret_key_fails_closed_not_with_the_wrong_code(settings):
    j = make(settings)
    j.site_access.record("Kestrel Industrial Estate", "Fire alarm panel - Kentec Syncro", "1234")
    j.settings.jarvis_secret_key = "a-different-key-entirely-1234567890"
    found = j.site_access.find("Kestrel")
    assert "couldn't decrypt" in found[0]["code"]
    await j.http.aclose()
