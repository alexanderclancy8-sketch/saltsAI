"""Azure runs Jarvis in UTC, but the code asks for "today"/"now" with plain date.today()/datetime.now() in dozens of
places. apply_timezone() makes the process's local time the business's (Europe/London), so they agree with the clock
on the wall instead of being an hour behind all summer."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

from jarvis.config import apply_timezone

needs_tzset = pytest.mark.skipif(not hasattr(time, "tzset") or not os.path.exists("/usr/share/zoneinfo/Europe/London"),
                                 reason="needs a POSIX tz database (runs in CI / on the server, not on Windows dev)")


@pytest.fixture
def restore_tz():
    old = os.environ.get("TZ")
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    if hasattr(time, "tzset"):
        time.tzset()


@needs_tzset
def test_naive_now_follows_london_including_daylight_saving(restore_tz):
    apply_timezone("UTC")
    apply_timezone("Europe/London")
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp()   # BST: UTC+1
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).timestamp()  # GMT: UTC+0
    assert datetime.fromtimestamp(summer).hour == 13
    assert datetime.fromtimestamp(winter).hour == 12


@needs_tzset
def test_a_just_after_midnight_london_moment_is_already_the_new_day(restore_tz):
    apply_timezone("Europe/London")
    # 23:30 UTC on 30 June is 00:30 BST on 1 July - date.today() must already say 1 July.
    moment = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc).timestamp()
    assert datetime.fromtimestamp(moment).date().isoformat() == "2026-07-01"


def test_is_a_no_op_where_the_platform_cannot_change_timezone(monkeypatch, restore_tz):
    monkeypatch.delattr(time, "tzset", raising=False)
    before = os.environ.get("TZ")
    apply_timezone("Europe/London")  # must not raise or touch the environment
    assert os.environ.get("TZ") == before


def test_blank_name_is_ignored(restore_tz):
    before = os.environ.get("TZ")
    apply_timezone("")
    assert os.environ.get("TZ") == before


# --- the suite must not inherit another test's timezone -------------------------------------------------------------
# Jarvis() applies the business timezone to the whole process. These two tests run in this order (pytest keeps file
# order): the first leaks it the way any test building a Jarvis() does, the second proves conftest's autouse
# `_isolate_process_timezone` put it back. Without that, every later test on a UTC runner in summer ran an hour ahead.
_TZ_AT_IMPORT = os.environ.get("TZ")
_SUMMER_NOON_UTC = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp()
_LOCAL_HOUR_AT_IMPORT = time.localtime(_SUMMER_NOON_UTC).tm_hour


def test_zz_1_building_jarvis_moves_the_process_timezone(settings):
    from jarvis.core import Jarvis
    Jarvis(settings)  # default timezone Europe/London -> sets os.environ["TZ"] where tzset exists
    if hasattr(time, "tzset") and os.path.exists("/usr/share/zoneinfo/Europe/London"):
        assert os.environ.get("TZ") == "Europe/London"
    else:
        os.environ["TZ"] = "Europe/London"  # Windows: apply_timezone is a no-op, so leak by hand to exercise the reset


def test_zz_2_the_timezone_is_back_to_what_it_was_before_the_previous_test():
    assert os.environ.get("TZ") == _TZ_AT_IMPORT
    assert time.localtime(_SUMMER_NOON_UTC).tm_hour == _LOCAL_HOUR_AT_IMPORT  # and the C-level zone too
