"""The terms an event is indexed under. The index and the full scan both
decide matches with ``terms_of``, so they cannot disagree about what an
event contains; only the index's own machinery (writing, lookup,
intersecting) can be wrong, and the tests compare it with the scan.

Two kinds of term:

- **Field terms**, ``f\\0path\\0value``: a field's exact value. Nested objects
  are flattened to dotted paths (``http.method``) and array elements are
  indexed under the array's path. Strings are lowercased, so ``level:info``
  matches "INFO"; numbers are written canonically, so ``status:200``
  matches 200, 200.0 and "200".
- **Text terms**, ``t\\0token``: every word (``\\w+``, lowercased) of every
  string and number, in any field, so a bare word matches anywhere.

The timestamp fields are left out: the time range already covers them, and
indexing one unique value per event would only bloat the dictionary.
"""

import re
from collections.abc import Iterator
from typing import Any

from .events import TIME_FIELDS

# Longer strings are not indexed as exact field values (they are still
# indexed word by word); a query for such a value is refused, not missed.
MAX_KEYWORD = 256
# Longer words are cut to this length, in the index and in queries alike.
MAX_TOKEN = 64

_WORD = re.compile(r"\w+")
# JSON's number syntax: what makes a query value numeric.
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


def field_key(path: str, value: str) -> str:
    return f"f\0{path}\0{value}"


def text_key(token: str) -> str:
    return f"t\0{token}"


def tokens(text: str) -> list[str]:
    return [token[:MAX_TOKEN] for token in _WORD.findall(text.lower())]


def canonical_number(value: int | float) -> str:
    """One spelling per number: 200, 200.0 and 2e2 are all "200"."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**63:
        return str(int(value))
    return repr(value) if isinstance(value, float) else str(value)


def query_values(raw: str) -> set[str]:
    """The indexed values a query value can match: itself, lowercased, and
    its canonical number if it is a JSON number ("7.0" matches "7.0" and 7;
    "007" is not a JSON number, so it matches only the string)."""
    values = {raw.lower()}
    if _NUMBER.fullmatch(raw):
        try:
            number = int(raw) if raw.lstrip("-").isdigit() else float(raw)
        except ValueError:  # an int over Python's 4,300-digit limit
            return values
        values.add(canonical_number(number))
    return values


def terms_of(event: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for path, value in _leaves(event):
        if isinstance(value, str):
            if len(value) <= MAX_KEYWORD:
                terms.add(field_key(path, value.lower()))
            terms.update(text_key(token) for token in tokens(value))
        elif isinstance(value, bool):
            terms.add(field_key(path, "true" if value else "false"))
        elif value is None:
            terms.add(field_key(path, "null"))
        elif isinstance(value, int | float):
            number = canonical_number(value)
            terms.add(field_key(path, number))
            terms.update(text_key(token) for token in tokens(number))
    return terms


def _leaves(event: dict[str, Any]) -> Iterator[tuple[str, object]]:
    """(dotted path, scalar) for every scalar, arrays expanded in place."""
    stack: list[tuple[str, object]] = [
        (key, value) for key, value in event.items() if key not in TIME_FIELDS
    ]
    while stack:
        path, value = stack.pop()
        if isinstance(value, dict):
            stack.extend((f"{path}.{key}", child) for key, child in value.items())
        elif isinstance(value, list):
            stack.extend((path, child) for child in value)
        else:
            yield path, value
