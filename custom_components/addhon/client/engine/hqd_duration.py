# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Programme duration of a washer on the HQD platform, and the delayed start built on it. Issue #131.

On HQD the hOn app does not read a programme's length off the catalogue's fixed
`remainingTime`: it evaluates the model's own formulas. `applianceModel.attributes`
carries `remainingTime = "remainingTimePrewash+remainingTimeMainWash+..."`, each term a
JSON tree keyed by `prCode` and by the options (`mainWashTime`, `rinseIterations`,
`spinSpeed`, ...), down to expressions such as `(rinseIterationTime+2)*(rinseIterations-1)`
and `#name` references to other attributes. The chain, top to bottom (apk2 decomp.txt):

    getProgramDuration            @2624910-2625244
    calculateOneProgramsDuration  @1401487-1401672
    getCorrectCycleDuration       @1401172-1401245
    getCycleDurationInSelection   @1403766-1404392  (HQD branch @1404041-1404102)
    getCommonCycleDuration        @1366766-1366807
    actualDuration (HQD)          @1365720-1366764  = remainingTimeGlobal @1366644
    lex / evaluate                @1366948-1367309 / @1367311-1367441

Why it matters: with `delayType: end` in the model attributes the appliance counts
`delayTime` to the END of the cycle. The app hides that behind "start at" / "end at":
when the platform is HQD, `delayType` is not 'start' and the brand is not Hoover it adds
the duration to the chosen start (@4077209-4077270, @4077643-4077650) and, on the
dashboard, puts the start at the end minus the duration (`HQDSchedule` @2586120).

A port of the reference implementation checked against the decompiled code executed
in Node: 0 differences over 40,000 random cases and 446 cases built from published
dumps (apk2/analysis/issue131-wm-delay/2-motore-durata-hqd.md, strumenti/duration/).
Values follow the JavaScript model the decompiled code runs on: objects are dicts with
string keys, numbers are floats, `_UNDEFINED` is `undefined` as opposed to None (null),
and the coercions that decide a result (`Number()`, truthiness, `String()`) are kept.
Only the HQD branch is ported: elsewhere the duration is None.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

# The washer types the delayed-start conversion concerns (issue #131 decision D4).
WASHER_TYPES = ("WM", "WD")
# The schema's own bound for `delayTime` (range 0..1410 step 30 on the real washers).
DELAY_TIME_MAX = 1410
# The app rounds a delayed start to half hours (`getRightDelayTime` @2646628).
_DELAY_STEP = 30

# JavaScript's `undefined`: a key that is not there, as opposed to a null value.
_UNDEFINED: Any = type("Undefined", (), {"__repr__": lambda self: "undefined"})()
_NAN = float("nan")

# What JavaScript's String.prototype.trim, Number() and `\s` treat as white space.
_JS_WHITESPACE = (
    "\t\n\x0b\x0c\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_WHITESPACE_RE = re.compile("[" + re.escape(_JS_WHITESPACE) + "]")
_DECIMAL = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)\Z")
_FLOAT_PREFIX = re.compile(
    r"[+-]?(?:Infinity|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
)
_DECIMAL_COMMA = re.compile(r"([0-9]),([0-9])")


class _JSError(Exception):
    """An exception the decompiled code throws (or the engine raises on its behalf)."""


class _JSTypeError(_JSError):
    pass


class _JSSyntaxError(_JSError):
    pass


class _JSRangeError(_JSError):
    pass


class _FormulaCycle(Exception):
    """Formulas that cite themselves: deliberately not a `_JSError`, so no try of the
    decompiled code absorbs it and the whole evaluation ends without a duration."""


# The app recurses until its JavaScript stack overflows, and the try that absorbs the
# overflow makes the result depend on the depth reached (decision H2 of 2026-10-16:
# no duration). The real formulas nest a handful of levels.
_MAX_DEPTH = 200


# --------------------------------------------------------------------------- JS values

def _is_number(value: Any) -> bool:
    return isinstance(value, float) or (isinstance(value, int) and not isinstance(value, bool))


def _js_value(value: Any) -> Any:
    """A Python / JSON value in the JavaScript model: integers become floats, keys strings."""
    if isinstance(value, bool) or value is None or value is _UNDEFINED or isinstance(value, str):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value
    if isinstance(value, (list, tuple)):
        return [_js_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _js_value(item) for key, item in value.items()}
    return str(value)


def _typeof(value: Any) -> str:
    if value is _UNDEFINED:
        return "undefined"
    if value is None or isinstance(value, (dict, list, _Duration)):
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if _is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    return "function"


def _truthy(value: Any) -> bool:
    if value is _UNDEFINED or value is None:
        return False
    if isinstance(value, bool):
        return value
    if _is_number(value):
        return not (value == 0 or math.isnan(value))
    if isinstance(value, str):
        return value != ""
    return True


def _strict_equal(left: Any, right: Any) -> bool:
    kind = _typeof(left)
    if kind != _typeof(right):
        return False
    if kind == "undefined":
        return True
    if kind == "object":
        return left is right
    return left == right


def _number_text(number: float) -> str:
    """`Number.prototype.toString()` in base 10."""
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    parts = Decimal(repr(abs(number))).as_tuple()
    digits = "".join(map(str, parts.digits)).rstrip("0") or "0"
    exponent = len(parts.digits) + parts.exponent
    count = len(digits)
    if count <= exponent <= 21:
        text = digits + "0" * (exponent - count)
    elif 0 < exponent <= 21:
        text = digits[:exponent] + "." + digits[exponent:]
    elif -6 < exponent <= 0:
        text = "0." + "0" * (-exponent) + digits
    else:
        power = exponent - 1
        text = (
            digits[0]
            + ("." + digits[1:] if count > 1 else "")
            + "e"
            + ("+" if power >= 0 else "-")
            + str(abs(power))
        )
    return sign + text


def _js_string(value: Any) -> str:
    if value is _UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if _is_number(value):
        return _number_text(float(value))
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None or item is _UNDEFINED else _js_string(item) for item in value)
    return "[object Object]"


def _js_number(value: Any) -> float:
    """`Number(value)`."""
    if value is _UNDEFINED:
        return _NAN
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if _is_number(value):
        return float(value)
    if isinstance(value, str):
        text = value.strip(_JS_WHITESPACE)
        if text == "":
            return 0.0
        if len(text) > 2 and text[0] == "0" and text[1] in "xXoObB":
            try:
                return float(int(text[2:], {"x": 16, "o": 8, "b": 2}[text[1].lower()]))
            except ValueError:
                return _NAN
        if _DECIMAL.match(text):
            return float(text.replace("Infinity", "inf"))
        return _NAN
    if isinstance(value, list):
        return _js_number(_js_string(value))
    return _NAN


def _js_isnan(value: Any) -> bool:
    """The global `isNaN`, which coerces."""
    return math.isnan(_js_number(value))


def _js_isfinite(value: Any) -> bool:
    """The global `isFinite`, which coerces."""
    return math.isfinite(_js_number(value))


def _number_isnan(value: Any) -> bool:
    """`Number.isNaN`, which does not."""
    return _is_number(value) and math.isnan(value)


def _js_round(number: float) -> float:
    """`Math.round`: halves go up, towards +Infinity."""
    if math.isnan(number) or math.isinf(number) or number == 0:
        return number
    floor = math.floor(number)
    return float(floor + 1 if number - floor >= 0.5 else floor)


def _parse_float(text: str) -> float:
    match = _FLOAT_PREFIX.match(text.lstrip(_JS_WHITESPACE))
    if not match:
        return _NAN
    return float(match.group(0).replace("Infinity", "inf"))


def _js_divide(left: float, right: float) -> float:
    try:
        return left / right
    except ZeroDivisionError:
        if math.isnan(left) or left == 0:
            return _NAN
        negative = (left < 0) != (math.copysign(1.0, right) < 0)
        return -math.inf if negative else math.inf


def _is_index(key: str) -> bool:
    return key.isdigit() and key.isascii() and str(int(key)) == key and int(key) < 4294967295


def _js_keys(obj: dict) -> list[str]:
    """Own keys in JavaScript's order: integer indices ascending, then insertion order."""
    indices = sorted((key for key in obj if _is_index(key)), key=int)
    return indices + [key for key in obj if not _is_index(key)]


def _property_key(key: Any) -> str:
    return key if isinstance(key, str) else _js_string(key)


def _get(obj: Any, key: Any) -> Any:
    """`obj[key]`, without the prototype chain except `length`."""
    if obj is None or obj is _UNDEFINED:
        raise _JSTypeError(f"reading {key!r} of {obj!r}")
    name = _property_key(key)
    if isinstance(obj, dict):
        return obj.get(name, _UNDEFINED)
    if isinstance(obj, (list, str)):
        if name == "length":
            return float(len(obj))
        if _is_index(name) and int(name) < len(obj):
            return obj[int(name)]
    return _UNDEFINED


def _optional(obj: Any, *keys: Any) -> Any:
    """`obj?.k1?.k2...`, the `== null` guards of the decompiled code."""
    for key in keys:
        if obj is None or obj is _UNDEFINED:
            return _UNDEFINED
        obj = _get(obj, key)
    return obj


def _nullish(value: Any) -> bool:
    return value is None or value is _UNDEFINED


def _object_keys(value: Any) -> list[str]:
    if value is None or value is _UNDEFINED:
        raise _JSTypeError("Object.keys of null or undefined")
    if isinstance(value, dict):
        return _js_keys(value)
    if isinstance(value, (list, str)):
        return [str(index) for index in range(len(value))]
    return []


def _for_in_keys(value: Any) -> list[str]:
    if value is None or value is _UNDEFINED:
        return []
    return _object_keys(value)


def _js_in(key: Any, obj: Any) -> bool:
    if not isinstance(obj, (dict, list)):
        raise _JSTypeError("'in' on a non-object")
    name = _property_key(key)
    if isinstance(obj, dict):
        return name in obj
    return name == "length" or (_is_index(name) and int(name) < len(obj))


def _has_own(obj: Any, key: Any) -> bool:
    if obj is None or obj is _UNDEFINED:
        raise _JSTypeError("hasOwnProperty of null or undefined")
    name = _property_key(key)
    if isinstance(obj, dict):
        return name in obj
    if isinstance(obj, (list, str)):
        return name == "length" or (_is_index(name) and int(name) < len(obj))
    return False


def _assign(*sources: Any) -> dict:
    """`Object.assign({}, ...sources)`."""
    out: dict = {}
    for source in sources:
        if isinstance(source, dict):
            for key in _js_keys(source):
                out[key] = source[key]
        elif isinstance(source, (list, str)):
            for index, item in enumerate(source):
                out[str(index)] = item
    return out


def _reject_constant(constant: str) -> Any:
    raise ValueError(constant)


def _json_parse(value: Any) -> Any:
    text = _js_string(value)
    try:
        return _js_value(
            json.loads(
                text,
                parse_int=float,
                parse_float=float,
                parse_constant=_reject_constant,
            )
        )
    except (ValueError, RecursionError) as error:
        raise _JSSyntaxError(str(error)) from None


def _json_has_pipe(value: Any) -> bool:
    """`JSON.stringify(value).includes('|')`."""
    if isinstance(value, str):
        return "|" in value
    if isinstance(value, dict):
        return any("|" in key or _json_has_pipe(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_json_has_pipe(item) for item in value)
    return False


def _replace_first(text: str, pattern: str, replacement: str) -> str:
    """`String.prototype.replace(string, string)`: first match, `$$ $& $\\` $'` patterns."""
    index = text.find(pattern)
    if index < 0:
        return text
    out: list[str] = []
    position = 0
    while position < len(replacement):
        char = replacement[position]
        if char == "$" and position + 1 < len(replacement):
            following = replacement[position + 1]
            if following == "$":
                out.append("$")
                position += 2
                continue
            if following == "&":
                out.append(pattern)
                position += 2
                continue
            if following == "`":
                out.append(text[:index])
                position += 2
                continue
            if following == "'":
                out.append(text[index + len(pattern):])
                position += 2
                continue
        out.append(char)
        position += 1
    return text[:index] + "".join(out) + text[index + len(pattern):]


def _need_string(value: Any, operation: str) -> str:
    if not isinstance(value, str):
        raise _JSTypeError(f"{operation} on {_typeof(value)}")
    return value


class _Duration:
    """The part of `moment.duration` the chain uses: milliseconds, as moment keeps them,
    so the float rounding of a sum agrees with the app's to the last digit."""

    def __init__(self, milliseconds: float, valid: bool = True) -> None:
        self.milliseconds = milliseconds
        self.valid = valid

    @property
    def minutes(self) -> float:
        return self.milliseconds / 6e4 if self.valid else _NAN

    def add_minutes(self, minutes: Any) -> "_Duration":
        self.milliseconds += float(minutes) * 6e4
        return self


def _moment_duration(value: Any, unit: Optional[str] = None) -> _Duration:
    """`moment.duration(value[, 'minutes'])` for the inputs the chain passes it.

    A Duration is copied; a number (or null, which `+input` makes 0) counts in the
    given unit, milliseconds without one; undefined is an empty duration.
    """
    if isinstance(value, _Duration):
        return _Duration(value.milliseconds, value.valid)
    if _is_number(value) or value is None:
        number = _js_number(value)
        factor = 6e4 if unit == "minutes" else 1.0
        return _Duration(number * factor, not math.isnan(number))
    if value is _UNDEFINED:
        return _Duration(0.0)
    raise _JSError(f"moment.duration({value!r})")


def _string_is_numeric(value: Any) -> bool:
    """`stringIsNumeric` @1365346-1365383."""
    kind = _typeof(value)
    if kind == "string":
        if len(value.strip(_JS_WHITESPACE)) < 1:  # @1365360-1365365
            return False
    elif kind != "number":  # @1365352-1365358
        return False
    number = _js_number(value)  # `r1 - 0` @1365370
    return not math.isnan(number) and math.isfinite(number)  # @1365367-1365379


# --------------------------------------------------------------------------- lexer

# Precedence and associativity @1366891-1366935.
_PRECEDENCE = {
    "+": (1, "left"),
    "-": (1, "left"),
    "*": (2, "left"),
    "/": (2, "left"),
    "(": (1, "left"),
    ")": (1, "left"),
}
_OPERATORS = ("+", "-", "*", "/")
# /#?([A-Za-z_]?)+[0-9]?[a-zA-Z_]+[0-9]?/g @1366955.
_IDENTIFIER = re.compile(r"#?[A-Za-z_]*[0-9]?[a-zA-Z_]+[0-9]?")


def _parse_int(text: str) -> float:
    """`Number.parseInt(text)` without a radix."""
    text = text.lstrip(_JS_WHITESPACE)
    sign = -1.0 if text[:1] == "-" else 1.0
    if text[:1] in "+-" and text[:1] != "":
        text = text[1:]
    if text[:2] in ("0x", "0X"):
        match = re.match(r"[0-9a-fA-F]+", text[2:])
        return sign * float(int(match.group(0), 16)) if match else _NAN
    match = re.match(r"[0-9]+", text)
    return sign * float(int(match.group(0))) if match else _NAN


def _lexer_is_number(char: Any) -> bool:
    """`isNumber` @1366849-1366868: a string tests `Number.isInteger(parseInt(c))`."""
    if isinstance(char, str):
        number = _parse_int(char)
        return math.isfinite(number) and number.is_integer()
    return _is_number(char) and math.isfinite(char) and float(char).is_integer()


def _lex(expression: Any, callback) -> list:
    """`lex` @1366948-1367309: identifiers replaced, then a shunting-yard on characters."""
    text = _WHITESPACE_RE.sub("", _need_string(expression, "replace"))  # removeSpaces
    tokens = _IDENTIFIER.findall(text)  # @1366956
    operators: list = []
    output: list = []
    buffer: list[str] = []
    if tokens:  # @1366963-1366975
        for token in tokens:  # @1366976-1366992
            value = callback(token)
            if _number_isnan(value):  # @1366979-1366983
                raise _JSRangeError("Value is NaN")  # @1367299-1367307
            text = _replace_first(text, token, _js_string(value))  # @1366984-1366988
    position = 0
    length = len(text)
    while position < length:  # @1366993-1367249
        char = text[position]
        if char == "-" and (position == 0 or text[position - 1] in ("+", "-", "*", "/", "(")):
            buffer = ["-"]  # unary minus @1367007-1367040, @1367234-1367241
            position += 1
            continue
        if _lexer_is_number(char) or char == ".":  # @1367041-1367048
            buffer.append(char)
            following = text[position + 1] if position + 1 < length else _UNDEFINED
            if not (_lexer_is_number(following) or following == "."):  # @1367053-1367062
                output.append(_parse_float("".join(buffer)))  # @1367063-1367072
                buffer = []
        if char in _OPERATORS:  # @1367073-1367089

            def dominates(top: str) -> bool:
                return _PRECEDENCE[top][0] > _PRECEDENCE[char][0] or (
                    _PRECEDENCE[top][0] == _PRECEDENCE[char][0]
                    and _PRECEDENCE[char][1] == "left"
                )

            if operators and dominates(operators[-1]) and operators[-1] != "(":
                while True:  # @1367131-1367178
                    popped = operators.pop()
                    if _truthy(popped):
                        output.append(popped)
                    if not operators:
                        break
                    if not (dominates(operators[-1]) and operators[-1] != "("):
                        break
            operators.append(char)  # @1367179-1367182
        if char == "(":  # @1367183-1367189
            operators.append(char)
        if char == ")" and operators:  # @1367190-1367198
            while True:  # @1367199-1367221
                popped = operators.pop()
                if not operators and popped != "(":
                    raise _JSSyntaxError("Mismatched parentheses")  # @1367225-1367233
                if popped == "(":
                    break
                if _truthy(popped):
                    output.append(popped)
                if not operators:
                    break
        position += 1
    while operators:  # @1367250-1367275
        popped = operators.pop()
        if _truthy(popped) and popped in ("(", ")"):
            raise _JSSyntaxError("Mismatched parentheses")  # @1367290-1367298
        if _truthy(popped):
            output.append(popped)
    if not output:
        raise _JSSyntaxError("Lexer error")  # @1367276-1367287
    return output


def _evaluate(tokens: list) -> float:
    """`evaluate` @1367311-1367441: an RPN stack; a missing or NaN operand skips the operator."""
    stack: list = []
    for token in tokens:  # @1367325-1367418
        if not _number_isnan(token) and _js_isfinite(token):  # @1367325-1367342
            stack.append(token)
            continue
        right = stack.pop() if stack else _UNDEFINED
        left = stack.pop() if stack else _UNDEFINED
        if (
            right is _UNDEFINED
            or _number_isnan(right)
            or left is _UNDEFINED
            or _number_isnan(left)
        ):  # @1367347-1367366
            continue
        if _strict_equal(token, "+"):
            stack.append(right + left)  # @1367407-1367412
        elif _strict_equal(token, "-"):
            stack.append(left - right)  # @1367400-1367406
        elif _strict_equal(token, "*"):
            stack.append(right * left)  # @1367393-1367399
        elif _strict_equal(token, "/"):
            stack.append(_js_divide(left, right))  # @1367386-1367392
    if len(stack) > 1:  # @1367419-1367422
        raise _JSError("Parsing error!")
    if stack and _typeof(stack[0]) == "number":  # @1367423-1367430
        return stack[0]
    raise _JSError("Parsing error!")  # @1367431-1367439


# --------------------------------------------------------------------------- actualDuration (HQD)

class _HqdEngine:
    """`actualDuration(params, anc, attributes, remainingTimes)` @1365720, its closure slots."""

    def __init__(self, params: Any, ancillary: Any, attributes: Any, remaining_times: Any) -> None:
        self._params = params
        self._ancillary = ancillary
        self._attributes = attributes
        self._remaining_times = remaining_times
        self._depth = 0

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > _MAX_DEPTH:
            raise _FormulaCycle

    def _break_remaining_times(self, formula: Any, params: Any) -> Any:
        """`breackRemainingTimes` @1365729-1365819."""
        try:
            parsed = _json_parse(formula)  # @1365740-1365745
        except _JSError:
            return formula if not _nullish(formula) else None  # @1365808-1365818
        keys = _object_keys(parsed)  # @1365746-1365751
        first = keys[0] if keys else _UNDEFINED
        wanted = _UNDEFINED if _nullish(params) else _get(params, first)  # @1365752-1365761
        branch = _get(parsed, first)  # @1365763
        if _has_own(branch, wanted):  # @1365765-1365769
            return _get(branch, wanted)  # @1365805-1365807
        for key in _for_in_keys(branch):  # @1365770-1365794
            if "|" in key and isinstance(wanted, str) and wanted in key.split("|"):
                return _get(branch, key)  # @1365790-1365793
        default = _get(branch, "default")  # @1365795-1365804
        return default if not _nullish(default) else None

    def _normalize(self, node: Any) -> Any:
        """`normalizeObject` @1365821-1365866."""
        keys = _object_keys(node)
        if len(keys) == 1 and _typeof(_get(node, keys[0])) == "string":  # @1365831-1365844
            text = _get(node, keys[0]).replace(";", ",")  # @1365848-1365856
            try:
                return _json_parse(text)
            except _JSError:
                return node  # @1365863
        return node

    def _resolve(self, name: Any, from_remaining_times: bool = False) -> Any:
        """`resolveParameters` @1365869-1366003."""
        if _typeof(name) == "string" and "#" in name:  # @1365887-1365895
            value = _UNDEFINED if _nullish(self._attributes) else _get(self._attributes, name[1:])
            resolved = value  # @1365960-1365970
            if _typeof(value) == "string" and value.startswith("{"):  # @1365971-1365980
                try:
                    resolved = _json_parse(resolved)
                except _JSError:
                    pass
            if _typeof(resolved) != "object":  # @1365991-1365998
                return resolved
            return self._search(resolved)  # @1365999-1366002
        if (
            from_remaining_times
            and not _nullish(self._remaining_times)
            and _truthy(_get(self._remaining_times, name))
        ):  # @1365896-1365905
            return self._normalize(_get(self._remaining_times, name))  # @1365948-1365959
        if not _nullish(self._params) and _truthy(_get(self._params, name)):  # @1365906-1365913
            return _get(self._params, name)  # @1365938-1365947
        if not _nullish(self._ancillary) and _truthy(_get(self._ancillary, name)):  # @1365914-1365920
            return _get(self._ancillary, name)  # @1365928-1365937
        return "round" if _strict_equal(name, "round") else _UNDEFINED  # @1365921-1365927

    @staticmethod
    def _numeric(value: Any) -> bool:
        """`isNumber` @1366006-1366016."""
        return not _js_isnan(value)

    @staticmethod
    def _number(value: Any, rounded: Any) -> float:
        return _js_round(_js_number(value)) if _truthy(rounded) else _js_number(value)

    def _search(self, node: Any, rounded: Any = _UNDEFINED) -> Any:
        """`recursiveSearch` @1366018-1366287: only the first key other than `round` counts."""
        self._enter()
        try:
            return self._search_once(node, rounded)
        finally:
            self._depth -= 1

    def _search_once(self, node: Any, rounded: Any) -> Any:
        rounding = rounded if rounded is not _UNDEFINED else False  # @1366029-1366039
        if _typeof(node) != "object":  # @1366040-1366044
            if self._numeric(node):  # @1366045-1366049
                return self._number(node, rounding)  # @1366092-1366109
            if _typeof(node) == "string":  # @1366050-1366054
                if "remainingTime" in node and "remainingTimeSoftening" not in node:
                    return self._search(self._split_remaining_time(node), rounding)  # @1366055-1366074
                result = self._calculate(node, rounding)  # @1366075-1366080
                if _truthy(result):
                    return _js_round(result) if _truthy(rounding) else result  # @1366081-1366091
            return 0.0  # @1366284-1366286
        for key in _object_keys(node):  # @1366110-1366123 (null raises TypeError)
            if key == "round":  # @1366124-1366126, @1366276-1366279
                rounding = True
                continue
            value = _get(node, key)  # @1366127-1366131
            resolved = self._resolve(key)  # @1366132-1366133
            if self._numeric(value):  # @1366134-1366136
                return self._number(value, rounding)  # @1366258-1366275
            if _typeof(value) != "string":  # @1366137-1366141
                if _typeof(resolved) != "undefined":  # @1366142-1366146
                    if _json_has_pipe(value):  # @1366147-1366155
                        chosen = self._search_right(resolved, value)  # @1366221-1366229
                        return self._search(_get(value, chosen), rounding)
                    if _js_in(resolved, value):  # @1366156-1366160
                        return self._search(_get(value, resolved), rounding)  # @1366211-1366220
                    if _js_in("default", value):  # @1366161-1366165
                        return self._search(_get(value, "default"), rounding)  # @1366202-1366210
                    if "suggestedLoad" in key and _truthy(resolved) and not _js_isnan(resolved):
                        return self._number(resolved, rounding)  # @1366166-1366197
                    return 0.0  # @1366198-1366201
                if _js_in("undefined", value):  # @1366233-1366240
                    return self._search(_get(value, "undefined"), rounding)  # @1366241-1366249
                return 0.0
            return self._calculate(value, rounding)  # @1366250-1366257
        return 0.0  # out of keys -> @1366284

    @staticmethod
    def _search_right(name: Any, node: Any) -> str:
        """`searchRightObject` @1366290-1366319: the LAST key containing the name."""
        found = ""
        needle = _js_string(name)
        for key in _object_keys(node):
            if needle in key:  # @1366306-1366310
                found = key
        return found

    def _split_remaining_time(self, text: str) -> Any:
        """`splitRemainingTimeParam` @1366321-1366358."""
        part = text.split("remainingTime")[1]  # @1366326-1366331
        name = part[:1].lower() + part[1:]  # @1366332-1366341
        table = self._resolve(name, True)  # @1366342-1366344
        value = self._resolve(name)  # @1366345
        if _truthy(value):
            return _UNDEFINED if _nullish(table) else _get(table, value)  # @1366346-1366354
        return value

    def _calculate(self, expression: Any, rounded: Any = _UNDEFINED) -> Any:
        """`calculateExpressions` @1366360-1366495; lexer errors propagate."""
        self._enter()
        try:
            return self._calculate_once(expression, rounded)
        finally:
            self._depth -= 1

    def _calculate_once(self, expression: Any, rounded: Any) -> Any:
        rounding = rounded if rounded is not _UNDEFINED else False  # @1366368-1366378
        text = _DECIMAL_COMMA.sub(r"\1.\2", _need_string(expression, "replace"))  # @1366379-1366383

        def callback(token: str) -> Any:  # @1366388-1366477
            if not _truthy(token):
                return _UNDEFINED
            if "#" in token:  # @1366392-1366397
                value = _UNDEFINED if _nullish(self._attributes) else _get(self._attributes, token[1:])
                if not _js_isnan(value):  # @1366434-1366436
                    return _js_number(value)  # @1366437-1366452
                try:
                    return self._search(_json_parse(value), rounding)  # @1366453-1366471
                except _JSError:
                    return _UNDEFINED  # @1366474-1366476
            resolved = self._resolve(token)  # @1366398-1366403
            if not _truthy(resolved) or _js_isnan(resolved):  # @1366404-1366410
                return _UNDEFINED
            return _js_number(resolved)  # @1366411-1366414

        tokens = _lex(text, callback)  # @1366384-1366481
        if not _truthy(tokens):
            return _UNDEFINED  # @1366482-1366483
        try:
            return _evaluate(tokens)
        except _JSError:
            return 0.0  # @1366490-1366493

    def _remaining_time_attribute(self) -> Any:
        """`getRemainingTimeAttribute` @1366497-1366539: remainingTime2, 3, then plain."""
        attributes = self._attributes
        chosen = _UNDEFINED if _nullish(attributes) else _get(attributes, "remainingTime")
        if not _nullish(attributes) and _truthy(_get(attributes, "remainingTime3")):
            chosen = _get(attributes, "remainingTime3")
        if not _nullish(attributes) and _truthy(_get(attributes, "remainingTime2")):
            chosen = _get(attributes, "remainingTime2")
        return self._break_remaining_times(chosen, self._params)  # @1366533-1366538

    def _validate(self, result: Any) -> Any:
        """`validateResult` @1366541-1366592: undefined, 0 and null fall back to the catalogue."""
        if result is not _UNDEFINED and not (_is_number(result) and result == 0) and result is not None:
            return result  # @1366543-1366554
        fallback = _optional(self._remaining_times, "remainingTime", "fixedValue")
        if not _truthy(fallback):
            fallback = _optional(self._remaining_times, "remainingTime", "defaultValue")
        return _js_number(fallback)  # @1366555-1366591

    @staticmethod
    def _single_value(tokens: Any) -> Any:
        """`checkValuesInLex` @1366594-1366641: exactly one number > 0 is the result."""
        positive = []
        if not _nullish(tokens):
            for token in tokens:  # @1366602-1366630
                if not _js_isnan(token) and _js_number(token) > 0:
                    positive.append(_js_number(token))
        if len(positive) != 1:
            return None  # @1366631-1366636
        return positive[0]

    def remaining_time_global(self) -> Any:
        """`remainingTimeGlobal` @1366644-1366760."""
        formula = self._remaining_time_attribute()  # outside the try @1366646-1366655
        text = formula
        if _truthy(formula):  # @1366656-1366660
            text = _DECIMAL_COMMA.sub(r"\1.\2", _need_string(formula, "replace"))

        def callback(token: str) -> Any:  # @1366669-1366722
            if not _truthy(token):
                return _UNDEFINED
            name = token.replace("#", "", 1)  # @1366675-1366679
            if not _nullish(self._attributes) and _truthy(_get(self._attributes, name)):
                term = _json_parse(_get(self._attributes, name))  # @1366680-1366715
            else:
                term = _UNDEFINED if _nullish(self._params) else _get(self._params, token)
                if not _truthy(term):  # @1366687-1366706
                    term = _UNDEFINED if _nullish(self._ancillary) else _get(self._ancillary, token)
            return self._search(term)  # @1366716-1366721

        try:
            tokens = _lex(text, callback)
        except _JSError:
            tokens = _UNDEFINED  # @1366726-1366728
        single = self._single_value(tokens)  # @1366729-1366734
        if _truthy(single):
            return single  # @1366757
        if not _truthy(tokens):
            return self._validate(None)  # @1366738-1366742
        result = _UNDEFINED
        try:
            result = _evaluate(tokens)
            return self._validate(result)  # @1366747-1366751
        except _JSError:
            return self._validate(result)  # @1366752-1366756


def _common_cycle_duration(params: Any, ancillary: Any, attributes: Any, remaining_times: Any):
    """`getCommonCycleDuration` @1366766-1366807: a Duration or None, all under try/catch.

    Formulas that cite themselves (`_FormulaCycle`, or a RecursionError on the way) are
    the JavaScript stack overflow, whose result in the app depends on the depth of its
    stack: no duration (issue #131 decision H2).
    """
    try:
        result = _HqdEngine(params, ancillary, attributes, remaining_times).remaining_time_global()
        if not _string_is_numeric(result):  # @1366779-1366785
            return None
        duration = _moment_duration(result, "minutes")  # @1366786-1366792
        if not duration.valid:  # @1366793-1366799
            return None
        return duration
    except (_JSError, RecursionError, _FormulaCycle):
        return None  # @1366802-1366805


# --------------------------------------------------------------------------- the chain above

def _map_appliance_attributes(rows: Any) -> dict:
    """`mapApplianceAttributes` @959839-959887: a `{parName, parValue}` list to an object."""
    if not isinstance(rows, list):
        return {}
    out: dict = {}
    for row in rows:
        if _truthy(row) and _truthy(_optional(row, "parName")) and _truthy(_optional(row, "parValue")):
            out[_property_key(_get(row, "parName"))] = _get(row, "parValue")  # @959854-959869
    return out


def _map_command_parameters(
    nodes: Any = _UNDEFINED,
    program_type: Any = _UNDEFINED,
    suggested: bool = False,
    raw: Any = _UNDEFINED,
) -> dict:
    """`mapCommandParameters` @1030332-1030507: schema nodes to the values they prescribe."""
    source = nodes if _truthy(nodes) else {}
    out: dict = {}
    for key in _object_keys(source):  # reduce @1030367-1030500
        node = _get(nodes, key)
        value = node
        if not _truthy(raw):
            if key in ("suggestedLoadW", "suggestedLoadWD", "suggestedLoadD") and suggested:
                value = _get(node, "suggestedValue")  # @1030382-1030389, @1030487-1030494
                if _nullish(value):
                    value = _get(node, "fixedValue")
            else:
                typology = _get(node, "typology")  # @1030390
                if not _strict_equal(typology, "range") and not _strict_equal(typology, "enum"):
                    if (
                        _strict_equal(typology, "fixed")
                        and not _nullish(node)
                        and _truthy(_get(node, "suggestedValue"))
                        and not _truthy(_get(node, "fixedValue"))
                    ):  # @1030402-1030421
                        value = _get(node, "suggestedValue")
                    else:  # @1030422-1030431
                        value = (
                            _get(node, "fixedValue")
                            if _strict_equal(_get(node, "typology"), "fixed")
                            else ""
                        )
                else:
                    done = False
                    if key in ("dryTime", "dryLevel") and _truthy(_get(node, "defaultValue")):
                        if _strict_equal(program_type, "W") or _strict_equal(program_type, "W+S"):
                            value = "0"  # @1030432-1030459
                            done = True
                    if not done:
                        if (
                            ("airWashPara" in key or "linkedSteamPrg" in key)
                            and not _nullish(node)
                            and _truthy(_get(node, "enumValues"))
                        ):
                            value = _get(node, "enumValues")  # @1030460-1030486
                        else:
                            default = _get(node, "defaultValue")
                            value = default if _truthy(default) else ""  # @1030476-1030483
        if _truthy(value):  # @1030495-1030498
            out[key] = value
    return out


_TD_WM_PREFIX = re.compile(r"^(TD|WM|Wm|Td|tD)")


def _strip_td_wm_keys(nodes: Any) -> Any:
    """`stripTDWMKeys` @1002450-1002527."""
    if not _truthy(nodes):
        return nodes
    keys = _object_keys(nodes)  # Object.entries @1002476-1002480
    if not keys:
        return nodes
    out: dict = {}
    for key in keys:
        value = _get(nodes, key)
        stripped = _TD_WM_PREFIX.sub("", key, count=1)
        if stripped[:1] == stripped[:1].upper():  # @1002497-1002508
            stripped = stripped[:1].lower() + stripped[1:]
        out[stripped] = value
    return out


def _temp_or_time_value(rules: Any, wanted: Any) -> Any:
    """`extractTempOrTimeValue` @1391671-1391733: the key listing the value, else '0'."""
    if _nullish(rules):
        return _UNDEFINED
    target = _js_string(wanted)
    found: Any = _UNDEFINED
    for key in _object_keys(rules):
        if target != "" and any(part == target for part in key.split("|")):
            found = key
            break
    if not _truthy(found):
        found = 0.0
    return _get(rules, found)


def _washing_type(program_type: Any, params: Any) -> Any:
    """`getWashingTypeFromParams` @1404597-1404667."""
    if _strict_equal(program_type, "D") or _strict_equal(program_type, "SD"):
        return program_type
    for key in ("dryLevel", "dryTime", "dryTimeMM"):
        if not _nullish(params) and _truthy(_get(params, key)) and not _strict_equal(_get(params, key), "0"):
            return "W+D"
    return "W"


def _cycle_duration_in_selection(
    appliance_type: Any,
    params: Any,
    ancillary: Any,
    catalogue: Any,
    model: Any,
    _synthetic: Any = _UNDEFINED,
    dry_off: Any = _UNDEFINED,
):
    """`getCycleDurationInSelection` @1403766-1404392, washer branch on HQD only.

    The ancillaries the engine reads are the CATALOGUE's (`catalogue.ancillaryParameters`),
    never the command's (@1404073-1404090). Off HQD the app sums the `remainingTimes`
    table (issue #99, `energy_label.actual_duration`); this port returns None there.
    """
    rules = _optional(
        catalogue, "ancillaryParameters", "programRules", "fixedValue", "addWashDefaultTime", "stainType"
    )  # @1403795-1403821
    if not _truthy(rules):
        rules = _optional(
            catalogue, "ancillaryParameters", "programRules", "enumValues", "addWashDefaultTime", "stainType"
        )  # @1403824-1403851
    selected = params
    if _js_number(_optional(params, "dryLevel")) > 0:  # @1403852-1403864
        selected = _assign(params, {"dryTime": "0"})  # @1403865-1403872
    program_type = _optional(catalogue, "ancillaryParameters", "programType", "fixedValue")
    attributes: Any = _UNDEFINED
    if (
        not _nullish(model)
        and not _nullish(_get(model, "applianceModel"))
        and _truthy(_get(_get(model, "applianceModel"), "attributes"))
    ):  # @1403889-1403901
        attributes = _map_appliance_attributes(_optional(model, "applianceModel", "attributes"))
    stain_minutes = 0.0
    if (
        _truthy(rules)
        and not _nullish(params)
        and _truthy(_get(params, "stainType"))
        and not _strict_equal(_optional(params, "stainType"), "0")
    ):  # @1403917-1403951
        stain_minutes = _js_number(
            _temp_or_time_value(rules, _js_number(_optional(params, "stainType")))
        )
    if appliance_type not in WASHER_TYPES:  # TD/DW/LC branches @1403952-1403966
        return None
    drying = any(_strict_equal(program_type, kind) for kind in ("SD", "WD", "W+D", "D"))
    timed_dry = _truthy(_optional(selected, "dryTime")) and not _strict_equal(
        _optional(selected, "dryTime"), "0"
    )  # @1403992-1404008
    selected = (
        _assign(selected, {"dryProgFlag": "1"}, {"dryLevel": "0"} if timed_dry else None)
        if drying
        else selected
    )  # @1403967-1404031
    if _truthy(dry_off):
        selected = _assign(selected, {"dryProgFlag": "0", "dryLevel": "0"})  # @1404032-1404040
    if _nullish(attributes) or not _truthy(_get(attributes, "platform")) or not _strict_equal(
        _optional(attributes, "platform"), "HQD"
    ):  # @1404041-1404056
        return None
    nodes = _optional(catalogue, "ancillaryParameters")
    mapped = _map_command_parameters({} if _nullish(nodes) else nodes, program_type, True)
    duration = _common_cycle_duration(
        selected, mapped, attributes, _optional(catalogue, "remainingTimes")
    )  # @1404073-1404102
    if _truthy(stain_minutes) and duration is not None:  # @1404375-1404383
        duration = duration.add_minutes(stain_minutes)
    return duration


def _one_programs_duration(argument: dict) -> dict:
    """`calculateOneProgramsDuration` @1401487-1401672: `{'standard': d[, 'W+D' | 'D': d]}`."""
    appliance_type = _get(argument, "applianceType")
    remaining_times = _get(argument, "remainingTimes")
    attributes = _get(argument, "attributes")
    options = _get(argument, "options")
    dictionary = _get(argument, "dictionaryParams")
    payload = _get(argument, "payload")
    if not _nullish(payload) and _truthy(_get(payload, "ancillaryParameters")):  # @1401499-1401521
        ancillary = _optional(payload, "ancillaryParameters")
    else:
        ancillary = _map_command_parameters(_optional(dictionary, "ancillaryParameters"))
    if not _nullish(payload) and _truthy(_get(payload, "parameters")):  # @1401522-1401538
        params = _optional(payload, "parameters")
    else:
        params = _map_command_parameters(_optional(dictionary, "parameters"))
    washing = _washing_type(_optional(ancillary, "programType"), params)  # @1401547-1401557
    catalogue = {
        "ancillaryParameters": _optional(dictionary, "ancillaryParameters"),
        "remainingTimes": remaining_times,
    }
    model = {"applianceModel": {"attributes": attributes}, "options": options}
    dry_off = _strict_equal(appliance_type, "WD") and _strict_equal(washing, "W+D")  # @1401573-1401586
    out = {
        "standard": _cycle_duration_in_selection(
            appliance_type, params, ancillary, catalogue, model, _UNDEFINED, dry_off
        )
    }  # @1401587-1401596
    if _strict_equal(washing, "W+D"):  # @1401597-1401602, @1401639-1401666
        out["W+D"] = _cycle_duration_in_selection(
            appliance_type, params, ancillary, dict(catalogue), dict(model)
        )
    elif _strict_equal(washing, "D"):  # @1401603-1401636
        out["D"] = _cycle_duration_in_selection(
            appliance_type, params, ancillary, dict(catalogue), dict(model)
        )
    return out


def _correct_cycle_duration(program_type: Any, durations: Any, washing_filter: bool):
    """`getCorrectCycleDuration` @1401172-1401245."""
    if not _truthy(durations):
        return None
    use_dry = (
        (_strict_equal(program_type, "W+D") or _strict_equal(program_type, "W+D+S"))
        and not washing_filter
        and _truthy(_get(durations, "W+D"))
    )
    if use_dry:
        key = "W+D"
    else:
        key = "D" if (_strict_equal(program_type, "D") and _truthy(_get(durations, "D"))) else "standard"
    chosen = _get(durations, key)
    return _moment_duration(chosen) if _truthy(chosen) else None


# --------------------------------------------------------------------------- addhOn side

def _attribute_rows(attributes: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Our flattened model attributes as the `{parName, parValue}` list the cloud sends."""
    return [{"parName": name, "parValue": value} for name, value in attributes.items()]


def programme_duration(
    appliance: Any,
    category: Any,
    parameters: Mapping[str, Any],
    ancillary: Optional[Mapping[str, Any]] = None,
) -> Optional[float]:
    """Minutes the app gives `category` run with `parameters`, or None. Issue #131.

    `getProgramDuration` with a command (@2625042-2625173): the command's own
    `parameters` and `ancillaryParameters` go in as the payload, the catalogue
    category's raw `ancillaryParameters` and `remainingTimes` groups as the dictionary,
    the model attributes as the formulas. None also for a result of zero or less, which
    the app would still use (decision H4 of 2026-10-16).
    """
    model_attributes = getattr(appliance, "model_attributes", None)
    if not isinstance(model_attributes, Mapping) or category is None:
        return None
    try:
        catalogue_ancillary = _js_value(getattr(category, "ancillary_group", None) or {})
        remaining_times = _js_value(getattr(category, "remaining_times", None))
        stripped = _strip_td_wm_keys(catalogue_ancillary)  # @2625002-2625011
        mapped = _map_command_parameters(stripped, _optional(stripped, "programType", "fixedValue"))
        params = _strip_td_wm_keys(_js_value(dict(parameters)))
        argument = {
            "applianceType": getattr(appliance, "appliance_type", ""),
            "dictionaryParams": {"ancillaryParameters": stripped if _truthy(stripped) else None},
            "payload": {
                # A command without ancillaries is `undefined` there, and the chain then
                # maps the catalogue's (@1401499-1401513).
                "ancillaryParameters": (
                    _strip_td_wm_keys(_js_value(dict(ancillary)))
                    if ancillary is not None
                    else _UNDEFINED
                ),
                "parameters": params,
            },
            "remainingTimes": remaining_times if _truthy(remaining_times) else None,
            "attributes": _js_value(_attribute_rows(model_attributes)),
            "options": _UNDEFINED,
        }  # @2625045-2625127
        durations = _one_programs_duration(argument)
        program_type = _optional(mapped, "programType")
        duration = _correct_cycle_duration(
            program_type, durations, _strict_equal(_washing_type(program_type, params), "W")
        )  # @2625153-2625173
    except (_JSError, RecursionError):
        return None
    except Exception:  # noqa: BLE001 - a shape this port did not foresee is no duration
        _LOGGER.debug("Programme duration: unreadable catalogue or formulas", exc_info=True)
        return None
    if duration is None or not duration.valid or not duration.minutes > 0:
        return None
    return duration.minutes


def delay_counts_to_end(appliance: Any) -> bool:
    """Whether this washer counts `delayTime` to the END of the cycle. Issue #131.

    The app's own test, the same on its start screen (@4077209-4077227) and on its
    dashboard (@2596540-2596556): platform HQD, `delayType` other than 'start', brand
    other than Hoover. Matched on a washer or washer-dryer only.
    """
    if getattr(appliance, "appliance_type", "") not in WASHER_TYPES:
        return False
    model = getattr(appliance, "model_attributes", None)
    if not isinstance(model, Mapping):
        return False
    if str(model.get("platform") or "").upper() != "HQD":
        return False
    if model.get("delayType") == "start":
        return False
    return str(getattr(appliance, "brand", "") or "").lower() != "hoover"


def delay_to_send(minutes_to_start: int, duration: float) -> tuple[int, bool]:
    """`delayTime` for a start `minutes_to_start` away, and whether 1410 capped it.

    `getRightDelayTime` @2646628 on the end of the cycle: the minutes from now,
    truncated as `moment.diff` truncates, at least 30, else rounded to the half hour
    the JavaScript way (decision D2); then the schema's bound (decision D7).
    """
    total = math.trunc(minutes_to_start + duration)
    sent = _DELAY_STEP if total < _DELAY_STEP else int(_js_round(total / _DELAY_STEP)) * _DELAY_STEP
    if sent > DELAY_TIME_MAX:
        return DELAY_TIME_MAX, True
    return sent, False


def _programme_key(program_name: Any) -> str:
    """A start's `programName` as the category key (`HonCommandLoader._clean_name`)."""
    name = str(program_name or "")
    return name.split(".")[-1].lower() if "PROGRAM" in name else name


def _same_code(left: Any, right: Any) -> bool:
    try:
        return float(str(left).strip()) == float(str(right).strip())
    except (TypeError, ValueError):
        return False


def started_programme_duration(appliance: Any, pr_code: Any) -> Optional[float]:
    """Duration of the cycle the appliance is waiting to run, or None. Issue #131.

    The newest `startProgram` of `/history` (decision D5), and only when its `prCode`
    is the shadow's: a start from the panel never reaches `/history`, and the newest
    row then belongs to an older cycle.
    """
    history = getattr(appliance, "command_history", None)
    if not isinstance(history, list):
        return None
    start = None
    for entry in history:
        command = entry.get("command") if isinstance(entry, dict) else None
        if isinstance(command, dict) and command.get("commandName") == "startProgram":
            start = command
            break
    if start is None:
        return None
    parameters = start.get("parameters")
    if not isinstance(parameters, dict) or not _same_code(parameters.get("prCode"), pr_code):
        return None
    commands = getattr(appliance, "commands", None)
    command = commands.get("startProgram") if isinstance(commands, Mapping) else None
    categories = getattr(command, "categories", None)
    if not isinstance(categories, Mapping):
        return None
    category = categories.get(_programme_key(start.get("programName")))
    ancillary = start.get("ancillaryParameters")
    return programme_duration(
        appliance,
        category,
        parameters,
        ancillary if isinstance(ancillary, dict) else None,
    )
