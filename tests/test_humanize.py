"""Cron and ISO timestamps are for APIs, not people - these renderers are what the owner actually sees or
hears instead, so they're worth pinning down directly."""

from __future__ import annotations

from jarvis.humanize import cron_to_english, human_datetime


def test_cron_common_patterns_read_naturally():
    assert cron_to_english("0 8 * * 1-5") == "every weekday at 8am"
    assert cron_to_english("*/30 * * * *") == "every 30 minutes"
    assert cron_to_english("0 */2 * * *") == "every 2 hours"
    assert cron_to_english("0 9 * * 6,0") == "every weekend at 9am"
    assert cron_to_english("30 14 * * 3") == "every Wednesday at 2:30pm"
    assert cron_to_english("0 8 1 * *") == "on the 1st of the month at 8am"
    assert cron_to_english("0 8 * * *") == "every day at 8am"


def test_cron_falls_back_to_naming_the_raw_schedule_when_it_cant_describe_it():
    assert cron_to_english("not a schedule") == "schedule 'not a schedule'"
    assert cron_to_english("0 8 15 6 *") == "schedule '0 8 15 6 *'"  # month-specific


def test_human_datetime_renders_uk_style():
    assert human_datetime("2026-10-06T09:00") == "Tuesday 6 October at 9am"
    assert human_datetime("2026-10-06") == "Tuesday 6 October"


def test_human_datetime_passes_through_blank_or_unparsable_values():
    assert human_datetime("") == ""
    assert human_datetime("next Tuesday") == "next Tuesday"
