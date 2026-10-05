import pytest

from jarvis.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Never pick up a developer's real .env during tests
    monkeypatch.chdir(tmp_path)
    return Settings(data_dir=tmp_path / "data", scheduler_enabled=False, anthropic_api_key="test",
                    web_search_enabled=True, _env_file=None)


@pytest.fixture(autouse=True)
def _isolate_process_timezone():
    """Jarvis() calls apply_timezone(), which sets os.environ["TZ"] and calls time.tzset() for the WHOLE process (no-op
    on Windows, real on Linux/CI). Left alone it would move datetime.now() by an hour part-way through the suite
    (Europe/London is UTC+1 in summer) for every later test. Put the zone back after every test."""
    import os
    import time
    had = "TZ" in os.environ
    old = os.environ.get("TZ")
    yield
    if had:
        os.environ["TZ"] = old
    else:
        os.environ.pop("TZ", None)
    if hasattr(time, "tzset"):
        time.tzset()
