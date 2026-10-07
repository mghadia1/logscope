"""Queries. Milestone 2's form is deliberately small: words separated by
spaces, all of which must match. Milestone 3 replaces the parser with a
real language (OR, NOT, parentheses, phrases, ranges); the terms stay.

- ``field:value`` matches events whose field equals value (lowercased, or as
  a number), e.g. ``level:error``, ``status:503``, ``http.method:get``.
- a bare word matches events with that word anywhere, e.g. ``timeout``.
  A word with several parts (``abc-123``) needs all of them.
"""

from dataclasses import dataclass

from .terms import MAX_KEYWORD, field_key, query_values, text_key, tokens


class QueryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FieldTerm:
    path: str
    # The index keys that satisfy it: any one of them.
    keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class TextTerm:
    # The index keys that satisfy it: all of them.
    keys: frozenset[str]


Term = FieldTerm | TextTerm


def parse(text: str) -> list[Term]:
    """All terms must match; an empty query matches every event."""
    terms: list[Term] = []
    for word in text.split():
        field, colon, value = word.partition(":")
        if colon:
            if not field or not value:
                raise QueryError(f"{word!r}: expected field:value")
            if len(value) > MAX_KEYWORD:
                raise QueryError(
                    f"{field}: values over {MAX_KEYWORD} characters are not"
                    " indexed exactly; search for words in it instead"
                )
            keys = frozenset(field_key(field, v) for v in query_values(value))
            terms.append(FieldTerm(field, keys))
        else:
            words = tokens(word)
            if not words:
                raise QueryError(f"{word!r} has no letters or digits to search for")
            terms.append(TextTerm(frozenset(text_key(w) for w in words)))
    return terms


def matches(event_terms: set[str], query: list[Term]) -> bool:
    """Whether an event with these terms matches; the full scan's test."""
    for term in query:
        if isinstance(term, FieldTerm):
            if term.keys.isdisjoint(event_terms):
                return False
        elif not term.keys <= event_terms:
            return False
    return True
