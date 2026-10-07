"""What an event is indexed under. The index and the full scan both decide
matches from ``terms_of`` (and, for phrases, ``word_lists``), so they cannot
disagree about what an event contains; only the index's own machinery
(writing, lookup, combining) can be wrong, and the tests compare it with
the scan.

Three kinds of term:

- **Field terms**, ``f\\0path\\0value``: a field's exact value. Nested objects
  are flattened to dotted paths (``http.method``) and array elements are
  indexed under the array's path. Strings are lowercased, so ``level:info``
  matches "INFO"; numbers are written canonically, and a string that is a
  number also gets its number's key, so ``status:200`` matches 200, 200.0,
  "200" and "200.0". Numeric ranges read these too.
- **Word terms**, ``t\\0word``: every word (``\\w+``, lowercased) of every
  string and number, in any field, so a bare word matches anywhere.
- **Exists terms**, ``e\\0path``: the field is present, for ``field:*``; also
  for every enclosing object (``http`` for ``http.method``).

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
# JSON's number syntax: what makes a value numeric.
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


def field_key(path: str, value: str) -> str:
    return f"f\0{path}\0{value}"


def field_prefix(path: str) -> str:
    """The start of every field key of ``path``; ranges scan these."""
    return f"f\0{path}\0"


def word_key(word: str) -> str:
    return f"t\0{word}"


EXISTS_PREFIX = "e\0"  # every exists key starts with it: the field list


def exists_key(path: str) -> str:
    return f"{EXISTS_PREFIX}{path}"


def tokens(text: str) -> list[str]:
    return [token[:MAX_TOKEN] for token in _WORD.findall(text.lower())]


def canonical_number(value: int | float) -> str:
    """One spelling per number: 200, 200.0 and 2e2 are all "200"."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**63:
        return str(int(value))
    return repr(value) if isinstance(value, float) else str(value)


def parse_number(text: str) -> int | float | None:
    """``text`` as a number if it is one in JSON's syntax, else None."""
    if not _NUMBER.fullmatch(text):
        return None
    try:
        return int(text) if text.lstrip("-").isdigit() else float(text)
    except ValueError:  # an int over Python's 4,300-digit limit
        return None


def query_values(raw: str) -> set[str]:
    """The indexed values a query value can match: itself, lowercased, and
    its canonical number if it is a JSON number ("7.0" matches "7.0" and 7;
    "007" is not a JSON number, so it matches only the string)."""
    values = {raw.lower()}
    number = parse_number(raw)
    if number is not None:
        values.add(canonical_number(number))
    return values


def terms_of(event: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for path, value in _leaves(event):
        terms.add(exists_key(path))
        dot = path.rfind(".")
        while dot != -1:  # every enclosing object exists too
            terms.add(exists_key(path[:dot]))
            dot = path.rfind(".", 0, dot)
        if isinstance(value, str):
            if len(value) <= MAX_KEYWORD:
                terms.add(field_key(path, value.lower()))
                # A string that is a number matches by value too, as it does
                # in a range: status:503 finds "503.0" as status>=503 does.
                number = parse_number(value)
                if number is not None:
                    terms.add(field_key(path, canonical_number(number)))
            terms.update(word_key(token) for token in tokens(value))
        elif isinstance(value, bool):
            terms.add(field_key(path, "true" if value else "false"))
        elif value is None:
            terms.add(field_key(path, "null"))
        elif isinstance(value, int | float):
            spelled = canonical_number(value)
            terms.add(field_key(path, spelled))
            terms.update(word_key(token) for token in tokens(spelled))
    return terms


def word_lists(event: dict[str, Any]) -> list[list[str]]:
    """The words of each string and number value, in order: a phrase must
    appear, consecutively, within one of them."""
    lists = []
    for _, value in _leaves(event):
        if isinstance(value, str):
            lists.append(tokens(value))
        elif isinstance(value, int | float) and not isinstance(value, bool):
            lists.append(tokens(canonical_number(value)))
    return lists


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
