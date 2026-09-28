import pytest

from jarvis.services.workspace import Workspace, WorkspaceError


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / ".git").mkdir()
    return Workspace(tmp_path)


def test_paths_are_confined(ws):
    for bad in ("/repo/../etc/passwd", "../../x", "/repo/.git/config", "/repo/src/../../../x"):
        with pytest.raises(WorkspaceError):
            ws.resolve(bad)
    assert ws.resolve("/repo/src/app.py").name == "app.py"


def test_edit_and_track_changes(ws):
    assert "return a - b" in ws.view("/repo/src/app.py")
    ws.str_replace("/repo/src/app.py", "return a - b", "return a + b")
    ws.create("/repo/tests/test_app.py", "from src.app import add\n")
    changed = ws.changed_files()
    assert changed["src/app.py"].endswith("return a + b\n")
    assert "tests/test_app.py" in changed
    assert "+    return a + b" in ws.diff()


def test_str_replace_requires_unique_match(ws):
    with pytest.raises(WorkspaceError):
        ws.str_replace("/repo/src/app.py", "nothing like this", "x")
    ws.create("/repo/dup.txt", "a\na\n")
    with pytest.raises(WorkspaceError):
        ws.str_replace("/repo/dup.txt", "a", "b")


def test_grep_and_insert(ws):
    assert "src/app.py:2" in ws.grep(r"return a")
    ws.run_editor_command({"command": "insert", "path": "/repo/src/app.py", "insert_line": 0, "insert_text": "# fixed"})
    assert ws.view("/repo/src/app.py", [1, 1]).strip().endswith("# fixed")
