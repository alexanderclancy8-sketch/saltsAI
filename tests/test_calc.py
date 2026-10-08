"""The safe calculator (jarvis/services/calc.py) and its `calculate` tool: exact decimal money arithmetic, the function whitelist, named
values, and a refusal - with a plain sentence - for every construct that isn't arithmetic. Nothing here uses eval/exec."""

from __future__ import annotations

import inspect
import time
from decimal import Decimal

import pytest

from jarvis import access
from jarvis.brain.tools import TOOLS_BY_NAME, CalculateIn, dispatch
from jarvis.services import async_tools, calc
from jarvis.services.calc import calculate, evaluate


def val(expr, **names):
    return calculate(expr, names or None)["value"]


# --------------------------------------------------------------------------- valid arithmetic and exactness
@pytest.mark.parametrize("expr,expected", [
    ("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("10 / 4", 2.5), ("10 // 4", 2), ("10 % 4", 2), ("2 ** 10", 1024), ("-5 + 8", 3),
    ("--5", 5), ("+5", 5), ("2 ** -1", 0.5), ("1_000 * 3", 3000), ("2e3 + 1", 2001), ("-7 // 2", -4), ("-7 % 3", 2), ("7 % -3", -2),
    ("round(2.5)", 2), ("round(3.5)", 4), ("round(1.23456, 3)", 1.24), ("abs(-4.5)", 4.5), ("min(3, 1, 2)", 1), ("max(3, 1, 2)", 3),
    ("sum(1, 2, 3.5)", 6.5), ("avg(2, 4, 9)", 5), ("sum([1, 2, 3], 4)", 10), ("avg([10, 20])", 15), ("max((1, 5, 2))", 5),
    ("pct(25, 200)", 12.5), ("pct_change(100, 150)", 50), ("pct_change(200, 150)", -25), ("pct_change(-100, -50)", 50),
    ("sqrt(144)", 12), ("sqrt(2) * sqrt(2)", 2),
])
def test_valid_expressions(expr, expected):
    assert val(expr) == expected


def test_money_arithmetic_is_exact_decimal_not_float():
    assert val("0.1 + 0.2") == 0.3
    assert calculate("0.1 + 0.2")["exact"] == "0.3"
    assert val("1.10 * 3") == 3.3
    assert val("19.99 * 3") == 59.97
    assert val("0.1 * 3 - 0.3") == 0           # a float gives 5.5e-17
    assert val("1234567.89 + 0.01") == 1234567.9
    assert calculate("1234567.891 * 1000")["text"] == "1,234,567,891.00"


def test_output_is_rounded_to_2dp_half_to_even_and_the_exact_value_is_kept():
    assert val("0.125") == 0.12 and val("0.135") == 0.14 and val("2.675") == 2.68 and val("-0.125") == -0.12
    r = calculate("pct_change(48200, 53910)")
    assert r["value"] == 11.85 and r["exact"].startswith("11.8464730290")
    assert "2 decimal places" in r["rounding"] and "half to even" in r["rounding"]
    assert calculate("1 / 3")["exact"] == "0.333333333333"
    assert isinstance(calculate("4 / 2")["value"], int)


def test_the_expression_is_echoed_back_exactly():
    r = calculate("  (1200 - 950) / 950 * 100 ")
    assert r["expression"] == "(1200 - 950) / 950 * 100" and r["value"] == 26.32


def test_named_values_are_the_only_names_and_are_decimal():
    assert val("revenue - cost", revenue=1234.56, cost="200.10") == 1034.46
    assert val("pct(part, whole) ", part=3, whole="12") == 25
    r = calculate("a + b", {"a": 1, "b": 2})
    assert r["named_values"] == {"a": 1, "b": 2}
    assert "isn't a number I know" in calculate("c + 1", {"a": 1})["error"]
    assert "isn't a number I know" in calculate("x")["error"]
    for bad in ({"a b": 1}, {"1a": 1}, {"round": 1}, {"__class__": 1}, {"a": "nope"}, {"a": float("nan")}, {"a": float("inf")},
                {"a": True}, {"a": None}, {"a": [1]}, {"a": 10 ** 70}):
        assert "error" in calculate("1", bad), bad
    assert "error" in calculate("1", {f"v{i}": i for i in range(calc.MAX_CONSTANTS + 1)})


# --------------------------------------------------------------------------- every refused construct
@pytest.mark.parametrize("expr,fragment", [
    ("abs.__class__", "Attribute access"), ("(1).real", "Attribute access"), ("a.b", "Attribute access"),
    ("[1, 2][0]", "Indexing"), ("'abc'[0]", "Indexing"),
    ("__import__('os').system('x')", "Only these functions"), ("open('f')", "isn't a function"), ("eval('1')", "isn't a function"),
    ("exec('1')", "isn't a function"), ("print(1)", "isn't a function"), ("len([1])", "isn't a function"), ("float(1)", "isn't a function"),
    ("int('3')", "isn't a function"), ("sum(1)(2)", "Only these functions"), ("(lambda: 1)()", "Only these functions"),
    ("getattr(1, 'x')", "isn't a function"), ("x.y(1)", "Only these functions"),
    ("[x for x in range(3)]", "Comprehensions"), ("sum(x for x in [1])", "Generator"), ("{x: 1 for x in [1]}", "Comprehensions"),
    ("{x for x in [1]}", "Comprehensions"), ("lambda: 1", "lambda"), ("lambda x: x", "lambda"),
    ("'a' + 'b'", "numbers only"), ("'1'", "numbers only"), ("b'1'", "numbers only"), ("f'{1}'", "f-strings"), ("1j", "numbers only"),
    ("True", "numbers only"), ("None", "None"), ("...", "numbers only"),
    ("1 < 2", "Comparisons"), ("1 == 1", "Comparisons"), ("1 and 2", "and/or"), ("1 or 2", "and/or"), ("not 1", "isn't allowed"),
    ("1 if 1 else 2", "Conditional"), ("{1: 2}", "Dictionaries"), ("{1, 2}", "Sets"), ("round(x=1)", "Keyword"),
    ("sum(*[1, 2])", "Star"), ("(y := 1)", "Assignments"), ("1 & 2", "Bitwise"), ("1 | 2", "Bitwise"), ("1 ^ 2", "Bitwise"),
    ("1 << 2", "Bitwise"), ("~1", "Bitwise"), ("1 @ 2", "Matrix"),
])
def test_every_non_arithmetic_construct_is_refused_with_a_sentence(expr, fragment):
    out = calculate(expr)
    assert "error" in out and "value" not in out, expr
    assert fragment in out["error"], (expr, out["error"])


@pytest.mark.parametrize("expr", ["import os", "x = 1", "1; 2", "del x", "for x in y: pass", "def f(): pass", "1 +", "(1", "1 2", "", "   ",
                                  "50%", "\x00", "@@", "1 +* 2"])
def test_statements_and_garbage_are_refused_not_crashed(expr):
    out = calculate(expr)
    assert "error" in out and "value" not in out


def test_percent_sign_hint():
    assert "pct(" in calculate("50%")["error"]


def test_the_module_never_calls_eval_or_exec_or_compile():
    import ast

    tree = ast.parse(inspect.getsource(calc))
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"eval", "exec", "compile", "__import__", "getattr", "open", "setattr", "globals", "locals"}, called
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} |         {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported <= {"__future__", "ast", "decimal", "re", "typing"}, imported


# --------------------------------------------------------------------------- denial-of-service caps
@pytest.mark.parametrize("expr", ["9**9**9", "9 ** 9 ** 9 ** 9", "2 ** 101", "2 ** -101", "10 ** 1000000", "(10**50) ** 100",
                                  "1e999", "1e61", "10**60 * 10", "999999999999 ** 5 * 99999999999 ** 5"])
def test_huge_numbers_and_exponents_are_refused_fast(expr):
    t0 = time.perf_counter()
    out = calculate(expr)
    assert "error" in out and "value" not in out, expr
    assert time.perf_counter() - t0 < 1.0


def test_exponent_boundary():
    assert val("2 ** 100") == 1267650600228229401496703205376
    assert "error" in calculate("2 ** 101")


def test_long_and_deep_expressions_are_refused():
    assert "too long" in calculate("1+" * 300 + "1")["error"]
    assert "too complicated" in calculate("+".join(["1"] * 150))["error"]          # 150 terms: > MAX_NODES nodes, under the length cap
    assert "nested too deeply" in calculate("-" * 150 + "1")["error"]
    assert "nested too deeply" in calculate("-(" * 110 + "1" + ")" * 110)["error"]
    assert "valid" in calculate("(" * 240 + "1" + ")" * 240)["error"]                # the parser's own nesting limit, caught
    assert "error" in calculate("[" * 100 + "1" + "]" * 100)
    assert "error" in calculate("sum([" + ",".join(["1"] * 201) + "])")
    assert val("(" * 30 + "1" + ")" * 30) == 1 and val("+".join(["1"] * 60)) == 60   # sensible sizes are fine


# --------------------------------------------------------------------------- division by zero and other maths errors
@pytest.mark.parametrize("expr,fragment", [
    ("1 / 0", "Division by zero"), ("1 // 0", "Division by zero"), ("1 % 0", "Division by zero"), ("5 / (3 - 3)", "Division by zero"),
    ("0 ** -1", "Division by zero"), ("pct(5, 0)", "whole is zero"), ("pct_change(0, 5)", "old value is zero"),
    ("sqrt(-1)", "negative"), ("avg()", "at least one"), ("sum()", "at least one"), ("min()", "at least one"), ("(-8) ** 0.5", "can't be calculated"),
    ("round(1, 2, 3)", "one or two"), ("round(1, 1.5)", "whole number"), ("round(1, 20)", "0 to 12"), ("abs(1, 2)", "exactly one"),
    ("pct(1)", "two numbers"), ("sqrt([1])", "single number"), ("1 + [1]", "single number"),
])
def test_maths_errors_say_what_is_wrong(expr, fragment):
    out = calculate(expr)
    assert "error" in out and fragment in out["error"], (expr, out)


def test_division_by_zero_message_names_the_operator_not_a_traceback():
    msg = calculate("(100 - 100) / (50 - 50)")["error"]
    assert "Division by zero" in msg and "Traceback" not in msg and "Decimal" not in msg and "'/'" in msg


def test_evaluate_returns_decimal_and_raises_calc_error():
    assert evaluate("1.5 * 2") == Decimal("3.0")
    with pytest.raises(calc.CalcError):
        evaluate("os.system('x')")


# --------------------------------------------------------------------------- the tool
def test_the_tool_is_registered_read_only_and_a_team_session_cannot_use_it():
    t = TOOLS_BY_NAME["calculate"]
    assert t.approval is False and set(CalculateIn.model_fields) == {"expression", "values"}
    assert "calculate" not in access.TEAM_TOOLS and not access.tool_allowed("calculate", access.Caller(access.TEAM, "Sam", "s"))
    assert access.tool_allowed("calculate", None) and access.tool_allowed("calculate", access.Caller(access.MANAGER))
    assert not async_tools.is_untrusted_output("calculate")        # its answer is just a number, not text from outside


async def test_the_tool_runs_through_dispatch(settings):
    from jarvis.core import Jarvis
    from tests.fakes import FakeClient

    j = Jarvis(settings, client=FakeClient())
    t = TOOLS_BY_NAME["calculate"]
    out = await dispatch(j, t, CalculateIn(expression="pct(a, b)", values={"a": 30, "b": 120}))
    assert out["value"] == 25 and out["expression"] == "pct(a, b)"
    bad = await dispatch(j, t, CalculateIn(expression="__import__('os')"))
    assert "error" in bad and j.db.pending_actions() == []
    await j.http.aclose()
