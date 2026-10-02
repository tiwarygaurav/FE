"""Decoder for SvelteKit's `devalue` wire format.

SvelteKit serialises server `load` results (``/<route>/__data.json``) and form-action
results with `devalue` (https://github.com/Rich-Harris/devalue). Instead of nested JSON,
the payload is a *flat array*: element 0 is the root value, and every object property /
array element is an integer index pointing at another slot of the same array. That makes
shared and cyclic references representable, but it means a plain ``json.loads`` gives you
something like ``[{"meterId": 1}, "J100000"]`` rather than ``{"meterId": "J100000"}``.

Only the subset that can appear in JSON-compatible payloads is needed here, but the common
tagged types (Date, Set, Map, BigInt, ...) are handled so an upstream change doesn't crash
the decoder.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

UNDEFINED = -1
HOLE = -2
NAN = -3
POSITIVE_INFINITY = -4
NEGATIVE_INFINITY = -5
NEGATIVE_ZERO = -6

_SPECIALS: dict[int, Any] = {
    UNDEFINED: None,
    HOLE: None,
    NAN: math.nan,
    POSITIVE_INFINITY: math.inf,
    NEGATIVE_INFINITY: -math.inf,
    NEGATIVE_ZERO: -0.0,
}

_UNSET = object()


class DevalueError(ValueError):
    """The payload is not valid devalue."""


def unflatten(values: Any) -> Any:
    """Rebuild the original value from a devalue flat array."""
    if isinstance(values, int):
        # A bare special value (e.g. the whole payload is `undefined`).
        if values in _SPECIALS:
            return _SPECIALS[values]
        raise DevalueError(f"invalid devalue payload: {values!r}")
    if not isinstance(values, list) or not values:
        raise DevalueError("devalue payload must be a non-empty array")

    hydrated: list[Any] = [_UNSET] * len(values)

    def hydrate(index: int) -> Any:
        if index in _SPECIALS:
            return _SPECIALS[index]
        if not 0 <= index < len(values):
            raise DevalueError(f"devalue index out of range: {index}")
        if hydrated[index] is not _UNSET:
            return hydrated[index]

        raw = values[index]
        if isinstance(raw, dict):
            obj: dict[str, Any] = {}
            hydrated[index] = obj  # register before recursing so cycles resolve
            for key, ref in raw.items():
                obj[key] = hydrate(ref)
            return obj
        if isinstance(raw, list):
            if raw and isinstance(raw[0], str):
                result = _hydrate_tagged(raw, hydrate)
                hydrated[index] = result
                return result
            arr: list[Any] = []
            hydrated[index] = arr
            arr.extend(hydrate(ref) for ref in raw)
            return arr
        hydrated[index] = raw  # str / int / float / bool / None
        return raw

    try:
        return hydrate(0)
    except DevalueError:
        raise
    except (TypeError, ValueError, IndexError, KeyError, AttributeError, RecursionError) as exc:
        raise DevalueError(f"malformed devalue payload: {type(exc).__name__}") from exc


def _hydrate_tagged(raw: list[Any], hydrate: Any) -> Any:
    tag, *rest = raw
    match tag:
        case "Date":
            return datetime.fromisoformat(rest[0].replace("Z", "+00:00"))
        case "Set":
            return [hydrate(ref) for ref in rest]
        case "Map":
            return {hydrate(k): hydrate(v) for k, v in zip(rest[::2], rest[1::2], strict=True)}
        case "BigInt":
            return int(rest[0])
        case "Object":  # boxed primitive
            return rest[0]
        case "null":  # object with a null prototype: ["null", key, ref, key, ref, ...]
            return {key: hydrate(ref) for key, ref in zip(rest[::2], rest[1::2], strict=True)}
        case "RegExp" | "URL" | "URLSearchParams":
            return rest[0]
        case _:
            raise DevalueError(f"unsupported devalue type tag: {tag!r}")
