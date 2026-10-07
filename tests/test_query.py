import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from logscope.query import (
    And,
    EventView,
    ExistsTerm,
    FieldTerm,
    Not,
    Or,
    PhraseTerm,
    QueryError,
    RangeTerm,
    WordTerm,
    matches,
    parse,
    render,
)

A, B, C = WordTerm("a"), WordTerm("b"), WordTerm("c")


@pytest.mark.parametrize(
    ("text", "tree"),
    [
        ("a b c", And((A, B, C))),
        ("a AND b", And((A, B))),
        ("a OR b c", Or((A, And((B, C))))),  # AND binds tighter than OR
        ("a b OR c", Or((And((A, B)), C))),
        ("NOT a b", And((Not(A), B))),  # NOT binds tightest
        ("NOT (a b)", Not(And((A, B)))),
        ("(a OR b) c", And((Or((A, B)), C))),
        ("a OR (b OR c)", Or((A, B, C))),  # flattened
        ("NOT NOT a", Not(Not(A))),
        (
            "a and b or not c",
            And((A, WordTerm("and"), B, WordTerm("or"), WordTerm("not"), C)),
        ),
    ],
)
def test_precedence_and_grouping(text: str, tree: object) -> None:
    assert parse(text) == tree


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("level:ERROR", FieldTerm("level", "ERROR")),
        ('route:"GET /users/{id}"', FieldTerm("route", "GET /users/{id}")),
        ("url:http://x/y", FieldTerm("url", "http://x/y")),  # first colon splits
        ('name:"say \\"hi\\""', FieldTerm("name", 'say "hi"')),
        ('star:"*"', FieldTerm("star", "*")),  # quoted: the value "*"
        ("http.method:*", ExistsTerm("http.method")),
        ("status>=500", RangeTerm("status", ">=", 500)),
        ("ms<2.5", RangeTerm("ms", "<", 2.5)),
        ("n>-1", RangeTerm("n", ">", -1)),
        ("t<=1e3", RangeTerm("t", "<=", 1000.0)),
        ("Timeout", WordTerm("timeout")),
        ("abc-123", PhraseTerm(("abc", "123"))),
        ('"upstream  Timeout"', PhraseTerm(("upstream", "timeout"))),
        ('"OR"', WordTerm("or")),  # quoted: a word, not the operator
    ],
)
def test_terms(text: str, term: object) -> None:
    assert parse(text) == term


def test_the_roadmap_example() -> None:
    query = parse(
        "level:error service:gatekeeper status>=500"
        ' "upstream timeout" NOT route:/healthz'
    )
    assert query == And(
        (
            FieldTerm("level", "error"),
            FieldTerm("service", "gatekeeper"),
            RangeTerm("status", ">=", 500),
            PhraseTerm(("upstream", "timeout")),
            Not(FieldTerm("route", "/healthz")),
        )
    )


@pytest.mark.parametrize(
    ("text", "message", "position"),
    [
        ("a (b c", "this ( is never closed", 2),
        ("a b) c", "this ) has no ( to close", 3),
        ("a ()", "empty parentheses", 2),
        ('msg:"oops', "this quote is never closed", 4),
        ("level: x", "expected a value after level:", 6),
        (":x", "expected a field name before :", 0),
        ("status>=abc", "a range needs a number after >=", 8),
        ("status:>=500", "write a range as status>=500, without the colon", 7),
        ("a AND", "AND needs a term after it", 2),
        ("OR a", "OR needs a term before it", 0),
        ("a OR OR b", "OR needs a term after it", 2),
        ("NOT", "NOT needs a term after it", 0),
        ("a (NOT) b", "NOT needs a term after it", 3),
        ("x ---", "'---' has no letters or digits to search for", 2),
        ("time:2026", "time is the event's time: search it with start and end", 0),
        ("msg:" + "x" * 300, "values over 256 characters", 4),
    ],
)
def test_errors_say_what_and_where(text: str, message: str, position: int) -> None:
    with pytest.raises(QueryError) as caught:
        parse(text)
    assert caught.value.message.startswith(message)
    assert caught.value.position == position


def test_an_empty_query_is_none() -> None:
    assert parse("") is None
    assert parse("   ") is None
    assert render(None) == ""


def test_render_makes_the_grouping_explicit() -> None:
    assert render(parse("a OR b c")) == "a OR (b c)"
    assert render(parse("NOT a OR b")) == "NOT a OR b"
    assert render(parse("NOT (a OR b)")) == "NOT (a OR b)"
    assert (
        render(parse('route:"GET /x" star:"*" n>=5.0'))
        == 'route:"GET /x" star:"*" n>=5'
    )


# Any query, printed back and parsed again, gives the same tree.
leaf = st.sampled_from(
    [
        "level:error",
        'route:"GET /users/{id}"',
        'q:"a \\"quoted\\" \\\\ value"',
        "http.method:*",
        "status>=500",
        "ms<2.5",
        "timeout",
        '"upstream timeout"',
        "abc-123",
        'star:"*"',
    ]
)
queries = st.recursive(
    leaf,
    lambda inner: st.one_of(
        st.lists(inner, min_size=2, max_size=3).map(" ".join),
        st.lists(inner, min_size=2, max_size=3).map(" AND ".join),
        st.lists(inner, min_size=2, max_size=3).map(" OR ".join),
        inner.map(lambda q: f"NOT {q}"),
        inner.map(lambda q: f"({q})"),
    ),
    max_leaves=8,
)


@settings(max_examples=300, deadline=None)
@given(queries)
def test_render_then_parse_gives_the_same_tree(text: str) -> None:
    tree = parse(text)
    assert parse(render(tree)) == tree


# Matching one event.

EVENT = EventView(
    {
        "level": "ERROR",
        "status": "503.0",
        "attempt": 2,
        "msg": "upstream timeout after retry",
        "note": "timeout upstream",
        "http": {"method": "GET", "tags": ["a", "b"]},
    }
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("level:error", True),
        ("status:503", True),  # "503.0" the string, as a number
        ("status>=503 status<504", True),
        ("status>503", False),
        ("attempt>=2", True),
        ("attempt>2", False),
        ('"upstream timeout"', True),
        ('"timeout after retry"', True),
        ('"retry upstream"', False),  # words present, not in that order
        ('"timeout timeout"', False),
        ('"upstream timeout after retry upstream"', False),  # across two fields
        ("http:*", True),
        ("http.tags:b", True),
        ("http.body:*", False),
        ("NOT level:info", True),
        ("level:info OR attempt:2", True),
        ("level:info OR (attempt:2 NOT http:*)", False),
    ],
)
def test_matching_one_event(text: str, expected: bool) -> None:
    assert matches(parse(text), EVENT) is expected
