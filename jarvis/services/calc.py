"""A safe calculator for Jarvis: arithmetic the model asks for, done exactly, WITHOUT running anything the model wrote.

The model hands over a short arithmetic expression such as ``pct_change(48200, 53910)`` or ``sum(a, b, c) * 1.2`` and gets back
a number. It is never ``eval``'d: the text is parsed with ``ast`` and walked by an evaluator that knows only these node types, so
there is nothing else to exploit:

* numbers (``12``, ``0.5``, ``1_000``, ``2e3``), parentheses, ``+ - * / // % **`` and unary ``-`` / ``+``;
* the functions in ``FUNCTIONS`` (``round abs min max sum avg pct pct_change sqrt``), called with positional arguments only,
  where ``min max sum avg`` also take ``[a, b, c]`` list/tuple literals;
* NAMES, only if the caller supplied them in the ``constants`` dict (a bare ``revenue`` is looked up there and nowhere else).

Everything else is refused with a plain sentence: attribute access, subscripts, calls to anything not listed (including calls of
a call or of an attribute), comprehensions, lambdas, strings, f-strings, comparisons, boolean logic, conditional expressions,
keyword arguments, ``*args``, walrus, ``await``/``yield``, imports, statements.

Money-safe: every number is a ``decimal.Decimal`` (precision 34), so ``0.1 + 0.2`` is exactly ``0.3``. The answer is rounded to
2 decimal places with round-half-to-even (banker's rounding, the same rule as ``round()``), and the unrounded value is returned too.

Denial-of-service caps: the expression is at most ``MAX_EXPR_CHARS`` long with at most ``MAX_NODES`` nodes nested ``MAX_DEPTH``
deep, a power's exponent is at most ``MAX_EXPONENT`` in size (so ``9**9**9`` is refused before anything is computed), no
number or result may be bigger than ``MAX_MAGNITUDE`` and list literals hold at most ``MAX_LIST`` items. Division by zero is
answered with a sentence naming the sum, not a traceback.
"""

from __future__ import annotations

import ast
import decimal
import re
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any, Callable

MAX_EXPR_CHARS = 500
MAX_NODES = 200
MAX_DEPTH = 100
MAX_EXPONENT = 100
MAX_MAGNITUDE = Decimal("1e60")
MAX_LIST = 200
MAX_CONSTANTS = 30
MAX_NAME_CHARS = 40
PRECISION = 34
OUTPUT_PLACES = Decimal("0.01")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")

ROUNDING_NOTE = ("Calculated exactly with decimal arithmetic, then rounded to 2 decimal places (half to even, so 0.125 -> 0.12 and "
                 "0.135 -> 0.14). 'exact' is the unrounded value.")


class CalcError(Exception):
    """The expression can't be calculated. The message is plain English and is what the model is told."""


def _bound(value: Decimal, what: str = "result") -> Decimal:
    if not value.is_finite():
        raise CalcError(f"The {what} isn't a finite number.")
    if abs(value) > MAX_MAGNITUDE:
        raise CalcError(f"The {what} is too large to calculate (more than 1e60).")
    return value


def _num(value: Any, what: str) -> Decimal:
    if isinstance(value, tuple):
        raise CalcError(f"{what} needs a single number, not a list.")
    return value


def _flatten(args: tuple, fn: str) -> list[Decimal]:
    out: list[Decimal] = []
    for a in args:
        out.extend(a if isinstance(a, tuple) else (a,))
    if not out:
        raise CalcError(f"{fn}() needs at least one number.")
    return out


def _fn_round(args: tuple) -> Decimal:
    if not 1 <= len(args) <= 2:
        raise CalcError("round(x) or round(x, decimals) takes one or two numbers.")
    x = _num(args[0], "round()")
    places = 0
    if len(args) == 2:
        d = _num(args[1], "round()")
        if d != d.to_integral_value() or not 0 <= d <= 12:
            raise CalcError("round(x, decimals): decimals must be a whole number from 0 to 12.")
        places = int(d)
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def _fn_abs(args: tuple) -> Decimal:
    if len(args) != 1:
        raise CalcError("abs(x) takes exactly one number.")
    return abs(_num(args[0], "abs()"))


def _fn_min(args: tuple) -> Decimal:
    return min(_flatten(args, "min"))


def _fn_max(args: tuple) -> Decimal:
    return max(_flatten(args, "max"))


def _fn_sum(args: tuple) -> Decimal:
    return sum(_flatten(args, "sum"), Decimal(0))


def _fn_avg(args: tuple) -> Decimal:
    values = _flatten(args, "avg")
    return sum(values, Decimal(0)) / len(values)


def _fn_pct(args: tuple) -> Decimal:
    if len(args) != 2:
        raise CalcError("pct(part, whole) takes two numbers and gives part as a percentage of whole.")
    part, whole = _num(args[0], "pct()"), _num(args[1], "pct()")
    if whole == 0:
        raise CalcError("pct(part, whole): the whole is zero, so a percentage is undefined.")
    return part / whole * 100


def _fn_pct_change(args: tuple) -> Decimal:
    if len(args) != 2:
        raise CalcError("pct_change(old, new) takes two numbers and gives the percentage change from old to new.")
    old, new = _num(args[0], "pct_change()"), _num(args[1], "pct_change()")
    if old == 0:
        raise CalcError("pct_change(old, new): the old value is zero, so a percentage change is undefined.")
    return (new - old) / abs(old) * 100


def _fn_sqrt(args: tuple) -> Decimal:
    if len(args) != 1:
        raise CalcError("sqrt(x) takes exactly one number.")
    x = _num(args[0], "sqrt()")
    if x < 0:
        raise CalcError("sqrt(x): x is negative, so there is no real square root.")
    return x.sqrt()


FUNCTIONS: dict[str, Callable[[tuple], Decimal]] = {
    "round": _fn_round, "abs": _fn_abs, "min": _fn_min, "max": _fn_max, "sum": _fn_sum, "avg": _fn_avg,
    "pct": _fn_pct, "pct_change": _fn_pct_change, "sqrt": _fn_sqrt,
}

_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_UNARY = (ast.USub, ast.UAdd)
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Call, ast.List, ast.Tuple, ast.Load,
                  *_BINOPS, *_UNARY)

# Plain-English refusals for the constructs people (and models) reach for. Anything else gets the generic wording.
_REFUSED = {
    ast.Attribute: "Attribute access (a.b) isn't allowed - this only does arithmetic.",
    ast.Subscript: "Indexing and slicing ([...]) aren't allowed - pass numbers directly, e.g. sum(1, 2, 3).",
    ast.Lambda: "lambda isn't allowed - this only does arithmetic.",
    ast.ListComp: "Comprehensions aren't allowed - list the numbers out, e.g. sum(1, 2, 3).",
    ast.SetComp: "Comprehensions aren't allowed - list the numbers out, e.g. sum(1, 2, 3).",
    ast.DictComp: "Comprehensions aren't allowed - list the numbers out, e.g. sum(1, 2, 3).",
    ast.GeneratorExp: "Generator expressions aren't allowed - list the numbers out, e.g. sum(1, 2, 3).",
    ast.JoinedStr: "Strings and f-strings aren't allowed - numbers only.",
    ast.Compare: "Comparisons aren't allowed - this returns a number, not true/false.",
    ast.BoolOp: "and/or aren't allowed - this only does arithmetic.",
    ast.IfExp: "Conditional expressions aren't allowed - this only does arithmetic.",
    ast.Dict: "Dictionaries aren't allowed - numbers only.",
    ast.Set: "Sets aren't allowed - use a list like [1, 2, 3].",
    ast.Starred: "Star-arguments (*x) aren't allowed.",
    ast.NamedExpr: "Assignments (:=) aren't allowed.",
    ast.Await: "await isn't allowed.",
    ast.Yield: "yield isn't allowed.",
    ast.YieldFrom: "yield isn't allowed.",
    ast.BitAnd: "Bitwise operators aren't allowed - use + - * / // % ** only.",
    ast.BitOr: "Bitwise operators aren't allowed - use + - * / // % ** only.",
    ast.BitXor: "Bitwise operators aren't allowed - use + - * / // % ** only.",
    ast.LShift: "Bitwise operators aren't allowed - use + - * / // % ** only.",
    ast.RShift: "Bitwise operators aren't allowed - use + - * / // % ** only.",
    ast.MatMult: "Matrix multiplication isn't allowed - use + - * / // % ** only.",
    ast.Invert: "Bitwise operators aren't allowed - use + - * / // % ** only.",
    ast.Not: "'not' isn't allowed - this only does arithmetic.",
}


def _structure_check(tree: ast.AST) -> None:
    """Walk the tree iteratively (so a deeply nested input can't exhaust the stack): size, depth and allowed node types."""
    count = 0
    stack: list[tuple[ast.AST, int]] = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES:
            raise CalcError(f"That expression is too complicated (more than {MAX_NODES} parts). Split it into smaller steps.")
        if depth > MAX_DEPTH:
            raise CalcError(f"That expression is nested too deeply (more than {MAX_DEPTH} levels). Split it into smaller steps.")
        if not isinstance(node, _ALLOWED_NODES):
            raise CalcError(_REFUSED.get(type(node), f"{type(node).__name__} isn't allowed - this only does arithmetic."))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise CalcError("Only these functions can be called, by name: " + ", ".join(FUNCTIONS) + ".")
            if node.func.id not in FUNCTIONS:
                raise CalcError(f"'{node.func.id[:40]}' isn't a function I can use. The only ones are: {', '.join(FUNCTIONS)}.")
            if node.keywords:
                raise CalcError("Keyword arguments aren't supported - pass the numbers in order, e.g. round(x, 2).")
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) > MAX_LIST:
            raise CalcError(f"A list can hold at most {MAX_LIST} numbers.")
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Name) and isinstance(node, ast.Call) and child is node.func:
                continue   # the function's own name is checked above, not looked up as a value
            stack.append((child, depth + 1))


def clean_constants(constants: dict[str, Any] | None) -> dict[str, Decimal]:
    """The caller's named numbers as Decimals. Names must be plain identifiers; values must be finite numbers (or numeric text)."""
    out: dict[str, Decimal] = {}
    if not constants:
        return out
    if len(constants) > MAX_CONSTANTS:
        raise CalcError(f"At most {MAX_CONSTANTS} named values can be given.")
    for name, value in constants.items():
        if not _NAME.fullmatch(str(name)):
            raise CalcError(f"'{str(name)[:40]}' isn't a usable name: use letters, digits and underscores, starting with a letter.")
        if name in FUNCTIONS:
            raise CalcError(f"'{name}' is the name of a function, so it can't also be a named value.")
        if isinstance(value, bool):
            raise CalcError(f"The value for '{name}' must be a number.")
        try:
            d = Decimal(str(value).strip().replace(",", "")) if isinstance(value, (str, int, float, Decimal)) else None
        except decimal.InvalidOperation:
            d = None
        if d is None or not d.is_finite():
            raise CalcError(f"The value for '{name}' must be a finite number.")
        out[str(name)] = _bound(d, f"value for '{name}'")
    return out


class _Evaluator:
    def __init__(self, source: str, constants: dict[str, Decimal]) -> None:
        self.source = source
        self.constants = constants

    def literal(self, node: ast.Constant) -> Decimal:
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            kind = "True/False" if isinstance(value, bool) else "Strings, bytes and complex numbers" if value is not None else "None"
            raise CalcError(f"{kind} isn't allowed - numbers only.")
        if isinstance(value, int):
            return _bound(Decimal(value), "number")
        text = ast.get_source_segment(self.source, node) or repr(value)
        try:
            d = Decimal(text.replace("_", ""))
        except decimal.InvalidOperation:
            d = Decimal(repr(value))
        return _bound(d, "number")

    def visit(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return self.visit(node.body)
        if isinstance(node, ast.Constant):
            return self.literal(node)
        if isinstance(node, ast.Name):
            if node.id not in self.constants:
                known = ", ".join(sorted(self.constants)[:10])
                raise CalcError(f"'{node.id[:40]}' isn't a number I know." +
                                (f" The named values given are: {known}." if known else " Use plain numbers (or pass named values)."))
            return self.constants[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            return tuple(_num(self.visit(e), "a list item") for e in node.elts)
        if isinstance(node, ast.UnaryOp):
            v = _num(self.visit(node.operand), "A sign")
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            return _bound(self.binop(node))
        if isinstance(node, ast.Call):
            args = tuple(self.visit(a) for a in node.args)
            return _bound(FUNCTIONS[node.func.id](args))   # type: ignore[union-attr]  (structure check proved .func is a Name)
        raise CalcError(f"{type(node).__name__} isn't allowed.")   # unreachable after _structure_check

    def binop(self, node: ast.BinOp) -> Decimal:
        a, b = _num(self.visit(node.left), "Arithmetic"), _num(self.visit(node.right), "Arithmetic")
        op = node.op
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, (ast.Div, ast.FloorDiv, ast.Mod)):
            if b == 0:
                sym = {ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%"}[type(op)]
                raise CalcError(f"Division by zero: the divisor of '{sym}' worked out as 0, so there is no answer. Check the "
                                "figure you are dividing by.")
            if isinstance(op, ast.Div):
                return a / b
            q = (a / b).to_integral_value(rounding=ROUND_FLOOR)   # Python's floor rule, not Decimal's truncation
            return q if isinstance(op, ast.FloorDiv) else a - b * q
        if isinstance(op, ast.Pow):
            if abs(b) > MAX_EXPONENT:
                raise CalcError(f"That power is too big to calculate (the exponent must be between -{MAX_EXPONENT} and {MAX_EXPONENT}).")
            if a == 0 and b < 0:
                raise CalcError("Division by zero: 0 can't be raised to a negative power.")
            return a ** b
        raise CalcError("That operator isn't allowed.")


def evaluate(expression: str, constants: dict[str, Any] | None = None) -> Decimal:
    """The exact value of ``expression``. Raises CalcError with a plain-English message for anything it won't or can't do."""
    if not isinstance(expression, str) or not expression.strip():
        raise CalcError("Give me an arithmetic expression, e.g. (1200 - 950) / 950 * 100.")
    if len(expression) > MAX_EXPR_CHARS:
        raise CalcError(f"That expression is too long ({len(expression)} characters; the limit is {MAX_EXPR_CHARS}). Split it up.")
    names = clean_constants(constants)
    source = expression.strip()
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError:
        hint = " For a percentage use pct(part, whole) or divide by 100 - '%' means remainder." if "%" in source else ""
        raise CalcError("That isn't a valid arithmetic expression (check the brackets and operators)." + hint) from None
    except (RecursionError, MemoryError, ValueError):   # ValueError: a null byte
        raise CalcError("That isn't a valid arithmetic expression.") from None
    _structure_check(tree)
    with decimal.localcontext() as ctx:
        ctx.prec = PRECISION
        ctx.Emax, ctx.Emin = 120, -120
        ctx.traps[decimal.InvalidOperation] = ctx.traps[decimal.DivisionByZero] = ctx.traps[decimal.Overflow] = True
        try:
            return _bound(_Evaluator(source, names).visit(tree))
        except decimal.DivisionByZero:
            raise CalcError("Division by zero somewhere in that expression, so there is no answer.") from None
        except decimal.InvalidOperation:
            raise CalcError("That can't be calculated (for example a negative number raised to a fractional power).") from None
        except decimal.Overflow:
            raise CalcError("The result is too large to calculate.") from None
        except RecursionError:
            raise CalcError("That expression is nested too deeply. Split it into smaller steps.") from None


def _exact_text(d: Decimal) -> str:
    """The unrounded value as plain text (no exponent), trimmed to what is meaningful."""
    text = format(d.normalize() if d != 0 else Decimal(0), "f")
    if "." in text:
        whole, frac = text.split(".")
        text = whole + "." + frac[:12].rstrip("0") if frac[:12].rstrip("0") else whole
    return text


def calculate(expression: str, constants: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the ``calculate`` tool returns: the value, the expression echoed back exactly, and a note on rounding. Never raises."""
    echoed = expression if isinstance(expression, str) else ""
    try:
        exact = evaluate(expression, constants)
    except CalcError as e:
        return {"error": str(e), "expression": echoed[:MAX_EXPR_CHARS]}
    rounded = exact.quantize(OUTPUT_PLACES, rounding=ROUND_HALF_EVEN) if abs(exact) < Decimal("1e30") else exact.to_integral_value()
    value: int | float = int(rounded) if rounded == rounded.to_integral_value() else float(rounded)
    out: dict[str, Any] = {"expression": echoed.strip(), "value": value,
                           "text": f"{rounded:,.2f}" if abs(rounded) < Decimal("1e30") else f"{rounded:,}",
                           "exact": _exact_text(exact), "rounding": ROUNDING_NOTE}
    if constants:
        out["named_values"] = {k: float(v) if v != v.to_integral_value() else int(v) for k, v in clean_constants(constants).items()}
    return out
