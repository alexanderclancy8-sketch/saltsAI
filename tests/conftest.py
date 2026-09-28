import pytest

from jarvis.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Never pick up a developer's real .env during tests
    monkeypatch.chdir(tmp_path)
    return Settings(data_dir=tmp_path / "data", scheduler_enabled=False, anthropic_api_key="test",
                    web_search_enabled=True, _env_file=None)
