"""Guard: no test module may read the wall clock at import time.

Why: a module-level ``TODAY = date.today()`` (or fixtures/constants built from it) is evaluated once at collection, so
it silently freezes "now" for the whole run and couples every fixture to the day the suite happens to start. Two
recurring "red on every PR" incidents came from this (fixtures built around a fixed date that the real clock later
moved past; fixtures built at import under one process timezone and used under another). Read the clock inside the
test / fixture, or pass ``today`` explicitly, or pin the module clock with monkeypatch.

What counts as "import time": module-level statements, class bodies, decorators, default argument values, and
comprehensions/lambdas' defaults at module level. Function and lambda BODIES are call time and are allowed. A module-level
call to a function of the same module (or one imported from another tests/ module) that itself reads the clock is
flagged too, because it builds the constant the same way.

Opt out on a specific line with ``# clock-ok: <reason>`` (a reason is mandatory), on the same line as the call or the
line directly above it.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

TESTS_DIR = Path(__file__).parent

# Fully-qualified callables that read the wall clock. Names are resolved through the module's imports, so
# ``from datetime import datetime as dt; dt.now()`` and ``import datetime as d; d.datetime.now()`` are both caught.
CLOCK_CALLS = {
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "datetime.datetime.today",
    "datetime.date.today",
    "time.time",
    "time.time_ns",
}
# These read the clock only when called without an explicit timestamp argument.
CLOCK_CALLS_WHEN_NO_ARGS = {"time.localtime", "time.gmtime", "time.ctime", "time.asctime"}

OPT_OUT = re.compile(r"#\s*clock-ok:\s*(\S.*)$")


def _opt_out_lines(source: str) -> set[int]:
    """Line numbers carrying a valid ``# clock-ok: <reason>`` comment."""
    lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and OPT_OUT.search(tok.string):
                lines.add(tok.start[0])
    except tokenize.TokenError:  # pragma: no cover - the file already parsed, so this should not happen
        pass
    return lines


class _Imports:
    """Maps local names to fully-qualified dotted names, from the module's import statements (any depth)."""

    def __init__(self, tree: ast.AST):
        self.names: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.names[a.asname] = a.name
                    else:  # `import datetime.x` binds the top package `datetime`
                        top = a.name.split(".")[0]
                        self.names[top] = top
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for a in node.names:
                    self.names[a.asname or a.name] = f"{node.module}.{a.name}"

    def resolve(self, func: ast.expr) -> str | None:
        parts: list[str] = []
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if not isinstance(func, ast.Name):
            return None
        base = self.names.get(func.id, func.id)
        return ".".join([base, *reversed(parts)])


def _is_clock_call(node: ast.Call, imports: _Imports) -> bool:
    name = imports.resolve(node.func)
    if name is None:
        return False
    if name in CLOCK_CALLS:
        return True
    return name in CLOCK_CALLS_WHEN_NO_ARGS and not node.args and not node.keywords


def _iter_import_time_nodes(tree: ast.Module):
    """Yield every node that executes at import time: everything except function/lambda bodies. Decorators,
    defaults and class bodies are included."""
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(node.decorator_list)
            stack.extend(node.args.defaults)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(node.args.defaults)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        stack.extend(ast.iter_child_nodes(node))


def _call_time_clock_functions(tree: ast.Module, imports: _Imports) -> set[str]:
    """Names of module-level functions whose body (transitively, within this module) reads the wall clock."""
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    direct = {name for name, fn in funcs.items()
              if any(isinstance(n, ast.Call) and _is_clock_call(n, imports) for n in ast.walk(fn))}
    reading = set(direct)
    changed = True
    while changed:
        changed = False
        for name, fn in funcs.items():
            if name in reading:
                continue
            called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if called & reading:
                reading.add(name)
                changed = True
    return reading


def find_violations(source: str, filename: str = "<test>", external_clock_funcs: dict[str, set[str]] | None = None
                    ) -> list[tuple[int, str]]:
    """Return [(lineno, description)] for import-time wall-clock reads in ``source``.

    ``external_clock_funcs`` maps a tests-module name (e.g. ``"tests.helpers"``) to the names in it that read the clock
    when called, so ``from tests.helpers import make_today`` followed by a module-level ``X = make_today()`` is caught.
    """
    tree = ast.parse(source, filename)
    imports = _Imports(tree)
    ok_lines = _opt_out_lines(source)
    local_reading = _call_time_clock_functions(tree, imports)
    external_reading = set()
    for local, qualified in imports.names.items():
        mod, _, attr = qualified.rpartition(".")
        if attr in (external_clock_funcs or {}).get(mod, ()):
            external_reading.add(local)

    out: list[tuple[int, str]] = []
    for node in _iter_import_time_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_clock_call(node, imports):
            what = imports.resolve(node.func)
        elif isinstance(node.func, ast.Name) and node.func.id in (local_reading | external_reading):
            what = f"{node.func.id}() (reads the clock when called)"
        else:
            continue
        first, last = node.lineno, getattr(node, "end_lineno", node.lineno)
        if any(ln in ok_lines for ln in range(first - 1, last + 1)):
            continue
        out.append((node.lineno, what))
    return sorted(set(out))


def _scan_tests_dir() -> dict[str, list[tuple[int, str]]]:
    files = sorted(TESTS_DIR.glob("*.py"))
    sources = {f: f.read_text(encoding="utf-8") for f in files}
    external: dict[str, set[str]] = {}
    for f, src in sources.items():
        tree = ast.parse(src, str(f))
        external[f"tests.{f.stem}"] = _call_time_clock_functions(tree, _Imports(tree))
    found = {}
    for f, src in sources.items():
        if f.name == Path(__file__).name:
            continue  # this file holds deliberately bad snippets as strings, never as code
        v = find_violations(src, str(f), external)
        if v:
            found[f.name] = v
    return found


def test_no_test_module_reads_the_wall_clock_at_import_time():
    found = _scan_tests_dir()
    report = "\n".join(f"  tests/{name}:{line}: {what}" for name, vs in found.items() for line, what in vs)
    assert not found, (
        "Test modules must not read the wall clock at import time (it freezes 'now' at collection and breaks once the "
        "real date or process timezone moves). Move the read into the test/fixture, take `today` as an argument, or "
        "monkeypatch the module clock. If it is genuinely safe, add `# clock-ok: <reason>` on that line.\n" + report)


# ---- the scanner itself -------------------------------------------------------------------------------------------

def _v(src: str, **kw):
    return [what for _, what in find_violations(src, **kw)]


def test_scanner_flags_module_level_clock_reads():
    assert _v("from datetime import date\nTODAY = date.today()\n") == ["datetime.date.today"]
    assert _v("import datetime\nNOW = datetime.datetime.now()\n") == ["datetime.datetime.now"]
    assert _v("import datetime as dt\nNOW = dt.datetime.utcnow()\n") == ["datetime.datetime.utcnow"]
    assert _v("from datetime import datetime as D\nNOW = D.now()\n") == ["datetime.datetime.now"]
    assert _v("import time\nT0 = time.time()\n") == ["time.time"]
    assert _v("import time\nfrom datetime import datetime\nX = datetime.fromtimestamp(time.time())\n") == ["time.time"]
    assert _v("import time\nT = time.localtime()\n") == ["time.localtime"]


def test_scanner_flags_clock_reads_in_class_bodies_decorators_defaults_and_comprehensions():
    assert _v("from datetime import date\nclass T:\n    d = date.today()\n")
    assert _v("import pytest\nfrom datetime import date\n@pytest.mark.parametrize('d', [date.today()])\n"
              "def test_x(d):\n    pass\n")
    assert _v("from datetime import date\ndef helper(d=date.today()):\n    return d\n")
    assert _v("from datetime import date\nDAYS = [date.today() for _ in range(3)]\n")
    assert _v("from datetime import date\nif True:\n    D = date.today()\n")


def test_scanner_allows_call_time_reads():
    assert not _v("from datetime import date\ndef helper():\n    return date.today()\n")
    assert not _v("from datetime import date\nimport pytest\n@pytest.fixture\ndef today():\n    return date.today()\n")
    assert not _v("from datetime import date\nclass T:\n    def test_x(self):\n        assert date.today()\n")
    assert not _v("from datetime import date\nf = lambda: date.today()\n")
    assert not _v("import time\nT = time.localtime(0)\n")  # explicit timestamp: no clock read
    assert not _v("from datetime import date\nD = date(2026, 10, 1)\n")  # fixed date is not a clock read
    assert not _v("class date:\n    @staticmethod\n    def today():\n        return 1\nD = date.today()\n")  # not datetime


def test_scanner_follows_module_level_calls_into_helpers_that_read_the_clock():
    src = ("from datetime import date\ndef _today():\n    return date.today()\ndef _tomorrow():\n    return _today()\n"
           "X = _tomorrow()\n")
    assert _v(src) == ["_tomorrow() (reads the clock when called)"]
    # defining/using the helper at call time is fine
    assert not _v("from datetime import date\ndef _today():\n    return date.today()\ndef test_x():\n    _today()\n")
    # helper imported from another tests module
    src = "from tests.helpers import make_today\nX = make_today()\n"
    assert _v(src, external_clock_funcs={"tests.helpers": {"make_today"}}) == ["make_today() (reads the clock when called)"]
    assert not _v(src, external_clock_funcs={"tests.helpers": set()})


def test_scanner_opt_out_needs_a_reason():
    ok = "from datetime import date\nTODAY = date.today()  # clock-ok: only used to print a banner\n"
    assert not _v(ok)
    assert not _v("from datetime import date\n# clock-ok: banner only\nTODAY = date.today()\n")
    assert _v("from datetime import date\nTODAY = date.today()  # clock-ok:\n")
    assert _v("from datetime import date\nTODAY = date.today()  # clock-ok\n")
    assert _v("from datetime import date\nTODAY = date.today()  # unrelated comment\n")
