"""The query language.

    level:error service:gatekeeper status>=500 "upstream timeout" NOT route:/healthz

Terms
    field:value       the field's whole value, case-insensitively; numbers by
                      value (status:503 matches 503, 503.0 and "503")
    field:"a value"   the same, quoted for spaces or ( ) " characters
    field:*           the field is present
    field>=N  >N  <=N  <N
                      a numeric range (numbers, and strings that are numbers)
    word              the word anywhere in the event; a word with several
                      parts (abc-123) is a phrase
    "a phrase"        the words anywhere, consecutively, within one value

Combining, tightest first
    NOT x             x does not match
    x y, x AND y      both match (AND is optional)
    x OR y            either matches
    ( ... )           grouping: a OR b c is a OR (b AND c)

AND, OR and NOT are operators only in capitals; "not" is a word. Errors
carry the position of the problem, so a client can point at it.
"""

import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from .events import TIME_FIELDS
from .terms import (
    MAX_KEYWORD,
    canonical_number,
    exists_key,
    field_key,
    field_prefix,
    parse_number,
    query_values,
    terms_of,
    tokens,
    word_key,
    word_lists,
)


class QueryError(ValueError):
    def __init__(self, message: str, position: int) -> None:
        super().__init__(f"{message} (at character {position})")
        self.message = message
        self.position = position


# The tree a query parses into.


@dataclass(frozen=True, slots=True)
class FieldTerm:
    path: str
    value: str  # as written, for showing the query back

    @property
    def keys(self) -> frozenset[str]:
        """Any one of these index keys matches (a union)."""
        return frozenset(field_key(self.path, v) for v in query_values(self.value))


@dataclass(frozen=True, slots=True)
class WordTerm:
    word: str

    @property
    def key(self) -> str:
        return word_key(self.word)


@dataclass(frozen=True, slots=True)
class PhraseTerm:
    words: tuple[str, ...]  # two or more

    @property
    def keys(self) -> frozenset[str]:
        """All of these are in every match, but having them all is not enough:
        they must also be consecutive, which only the event can tell."""
        return frozenset(word_key(w) for w in self.words)


@dataclass(frozen=True, slots=True)
class ExistsTerm:
    path: str

    @property
    def key(self) -> str:
        return exists_key(self.path)


@dataclass(frozen=True, slots=True)
class RangeTerm:
    path: str
    op: str  # ">=", ">", "<=", "<"
    bound: int | float

    @property
    def prefix(self) -> str:
        return field_prefix(self.path)

    def accepts(self, value: str) -> bool:
        """Whether an indexed field value (the text after the prefix) is a
        number in the range."""
        number = parse_number(value)
        if number is None:
            return False
        match self.op:
            case ">=":
                return number >= self.bound
            case ">":
                return number > self.bound
            case "<=":
                return number <= self.bound
            case _:
                return number < self.bound


@dataclass(frozen=True, slots=True)
class Not:
    child: Node


@dataclass(frozen=True, slots=True)
class And:
    children: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Or:
    children: tuple[Node, ...]


Node = FieldTerm | WordTerm | PhraseTerm | ExistsTerm | RangeTerm | Not | And | Or


# Lexing


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str  # "word", "quoted", "(", ")"
    text: str  # a quoted token's text is unescaped
    start: int
    end: int


def _lex(text: str) -> list[_Token]:
    found = []
    i = 0
    while i < len(text):
        char = text[i]
        if char.isspace():
            i += 1
        elif char in "()":
            found.append(_Token(char, char, i, i + 1))
            i += 1
        elif char == '"':
            j = i + 1
            chars = []
            while j < len(text) and text[j] != '"':
                if text[j] == "\\" and j + 1 < len(text):
                    j += 1  # \" and \\ (any escaped character stands for itself)
                chars.append(text[j])
                j += 1
            if j == len(text):
                raise QueryError("this quote is never closed", i)
            found.append(_Token("quoted", "".join(chars), i, j + 1))
            i = j + 1
        else:
            j = i
            while j < len(text) and not text[j].isspace() and text[j] not in '()"':
                j += 1
            found.append(_Token("word", text[i:j], i, j))
            i = j
    return found


# Parsing

_FIELD_OPERATOR = re.compile(r"[:<>]")


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _lex(text)
        self.i = 0

    def peek(self) -> _Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> _Token:
        token = self.tokens[self.i]
        self.i += 1
        return token

    @staticmethod
    def is_operator(token: _Token | None, *names: str) -> bool:
        return token is not None and token.kind == "word" and token.text in names

    def needs_operand(self, operator: _Token) -> None:
        """After AND, OR or NOT there must be a term."""
        following = self.peek()
        if (
            following is None
            or following.kind == ")"
            or self.is_operator(following, "AND", "OR")
        ):
            raise QueryError(f"{operator.text} needs a term after it", operator.start)

    def parse(self) -> Node | None:
        if not self.tokens:
            return None
        node = self.parse_or()
        leftover = self.peek()
        if leftover is not None:  # only a ) can stop parse_or early
            raise QueryError("this ) has no ( to close", leftover.start)
        return node

    def parse_or(self) -> Node:
        children = [self.parse_and()]
        while self.is_operator(self.peek(), "OR"):
            self.needs_operand(self.take())
            children.append(self.parse_and())
        return _combine(Or, children)

    def parse_and(self) -> Node:
        children = [self.parse_unary()]
        while True:
            token = self.peek()
            if token is None or token.kind == ")" or self.is_operator(token, "OR"):
                return _combine(And, children)
            if self.is_operator(token, "AND"):
                self.needs_operand(self.take())
            children.append(self.parse_unary())

    def parse_unary(self) -> Node:
        token = self.take()
        if self.is_operator(token, "NOT"):
            self.needs_operand(token)
            return Not(self.parse_unary())
        if self.is_operator(token, "AND", "OR"):
            raise QueryError(f"{token.text} needs a term before it", token.start)
        if token.kind == ")":
            raise QueryError("this ) has no ( to close", token.start)
        if token.kind == "(":
            following = self.peek()
            if following is not None and following.kind == ")":
                raise QueryError("empty parentheses", token.start)
            if following is None:
                raise QueryError("this ( is never closed", token.start)
            node = self.parse_or()
            close = self.peek()
            if close is None or close.kind != ")":
                raise QueryError("this ( is never closed", token.start)
            self.take()
            return node
        if token.kind == "quoted":
            return _words(token.text, token.start)
        return self.word(token)

    def word(self, token: _Token) -> Node:
        found = _FIELD_OPERATOR.search(token.text)
        if found is None:
            return _words(token.text, token.start)
        cut = found.start()
        path = token.text[:cut]
        if not path:
            raise QueryError(
                f"expected a field name before {token.text[cut]}", token.start
            )
        if path in TIME_FIELDS:
            raise QueryError(
                f"{path} is the event's time: search it with start and end", token.start
            )
        if token.text[cut] == ":":
            value = token.text[cut + 1 :]
            value_start = token.start + cut + 1
            if value == "":
                quoted = self.peek()
                if (
                    quoted is None
                    or quoted.kind != "quoted"
                    or quoted.start != token.end
                ):
                    raise QueryError(f"expected a value after {path}:", token.end)
                self.take()
                return _field(path, quoted.text, quoted.start)
            if value == "*":
                return ExistsTerm(path)
            if value[0] in "<>":
                # Other tools write ranges this way; here it would silently be
                # an exact match on the text ">=500".
                raise QueryError(
                    f"write a range as {path}{value}, without the colon", value_start
                )
            return _field(path, value, value_start)
        op = (
            token.text[cut : cut + 2]
            if token.text[cut + 1 : cut + 2] == "="
            else token.text[cut]
        )
        value = token.text[cut + len(op) :]
        number = parse_number(value)
        if number is None:
            raise QueryError(
                f"a range needs a number after {op}", token.start + cut + len(op)
            )
        return RangeTerm(path, op, number)


def _field(path: str, value: str, position: int) -> FieldTerm:
    if len(value) > MAX_KEYWORD:
        raise QueryError(
            f"values over {MAX_KEYWORD} characters are not indexed exactly;"
            " search for words in it instead",
            position,
        )
    return FieldTerm(path, value)


def _words(text: str, position: int) -> Node:
    words = tuple(tokens(text))
    if not words:
        raise QueryError(f"{text!r} has no letters or digits to search for", position)
    return WordTerm(words[0]) if len(words) == 1 else PhraseTerm(words)


def _combine(kind: type[And] | type[Or], children: list[Node]) -> Node:
    """One child stands alone; nested ANDs (or ORs) are flattened."""
    if len(children) == 1:
        return children[0]
    flat: list[Node] = []
    for child in children:
        flat.extend(child.children if isinstance(child, kind) else [child])
    return kind(tuple(flat))


def parse(text: str) -> Node | None:
    """The query's tree; None for an empty query (it matches everything)."""
    return _Parser(text).parse()


# Showing a query back, with its grouping made explicit


_PLAIN = re.compile(r'[^\s()"\\]+')


def render(node: Node | None) -> str:
    match node:
        case None:
            return ""
        case FieldTerm(path, value):
            plain = _PLAIN.fullmatch(value) and value != "*"
            return f"{path}:{value}" if plain else f"{path}:{_quote(value)}"
        case WordTerm(word):
            return word
        case PhraseTerm(words):
            return _quote(" ".join(words))
        case ExistsTerm(path):
            return f"{path}:*"
        case RangeTerm(path, op, bound):
            return f"{path}{op}{canonical_number(bound)}"
        case Not(child):
            return f"NOT {_grouped(child)}"
        case And(children):
            return " ".join(_grouped(child) for child in children)
        case Or(children):
            return " OR ".join(_grouped(child) for child in children)
    raise TypeError(node)


def _grouped(node: Node) -> str:
    return f"({render(node)})" if isinstance(node, And | Or) else render(node)


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# Deciding whether one event matches: the full scan's test, and the check
# of the index's candidates when the index alone cannot be sure.


class EventView:
    """An event, with what matching needs worked out once, on demand."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    @cached_property
    def terms(self) -> set[str]:
        return terms_of(self.data)

    @cached_property
    def word_lists(self) -> list[list[str]]:
        return word_lists(self.data)


def matches(node: Node | None, event: EventView) -> bool:
    match node:
        case None:
            return True
        case FieldTerm():
            return not node.keys.isdisjoint(event.terms)
        case WordTerm() | ExistsTerm():
            return node.key in event.terms
        case PhraseTerm(words):
            return any(_consecutive(words, value) for value in event.word_lists)
        case RangeTerm():
            prefix = node.prefix
            return any(
                node.accepts(term[len(prefix) :])
                for term in event.terms
                if term.startswith(prefix)
            )
        case Not(child):
            return not matches(child, event)
        case And(children):
            return all(matches(child, event) for child in children)
        case Or(children):
            return any(matches(child, event) for child in children)
    raise TypeError(node)


def _consecutive(phrase: tuple[str, ...], words: list[str]) -> bool:
    size = len(phrase)
    return any(
        tuple(words[i : i + size]) == phrase for i in range(len(words) - size + 1)
    )
