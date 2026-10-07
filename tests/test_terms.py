import pytest

from logscope.query import FieldTerm, QueryError, TextTerm, matches, parse
from logscope.terms import MAX_TOKEN, field_key, query_values, terms_of, text_key


def test_field_values_are_lowercased_and_numbers_canonical() -> None:
    terms = terms_of({"level": "INFO", "status": 200, "ratio": 2.0, "ms": 1.5})
    assert field_key("level", "info") in terms
    assert field_key("status", "200") in terms
    assert field_key("ratio", "2") in terms  # 2.0 and 2 are one number
    assert field_key("ms", "1.5") in terms


def test_nested_objects_flatten_and_arrays_expand() -> None:
    terms = terms_of(
        {"http": {"method": "GET", "headers": {"host": "a"}}, "tags": ["x", "y"]}
    )
    assert field_key("http.method", "get") in terms
    assert field_key("http.headers.host", "a") in terms
    assert {field_key("tags", "x"), field_key("tags", "y")} <= terms


def test_booleans_and_null() -> None:
    terms = terms_of({"ok": True, "user": None})
    assert {field_key("ok", "true"), field_key("user", "null")} <= terms


def test_words_of_every_string_and_number_are_text_terms() -> None:
    terms = terms_of(
        {"msg": "Upstream timeout after 5s", "job_id": "abc-123", "status": 504}
    )
    for word in ["upstream", "timeout", "after", "5s", "abc", "123", "504"]:
        assert text_key(word) in terms


def test_timestamp_fields_are_not_indexed() -> None:
    terms = terms_of({"time": "2026-10-06T21:00:00Z", "ts": 1, "msg": "hi"})
    assert terms == {field_key("msg", "hi"), text_key("hi")}


def test_long_strings_are_words_only_and_long_words_are_cut() -> None:
    long_value = "word " * 100
    terms = terms_of({"msg": long_value, "blob": "x" * 100})
    assert not any(t.startswith("f\0msg\0") for t in terms)
    assert text_key("word") in terms
    assert text_key("x" * MAX_TOKEN) in terms


def test_query_values_match_a_string_and_its_number() -> None:
    assert query_values("7.0") == {"7.0", "7"}
    assert query_values("007") == {"007"}  # not JSON number syntax: a string
    assert query_values("2e2") == {"2e2", "200"}
    assert query_values("ERROR") == {"error"}
    assert query_values("1" * 5000) == {"1" * 5000}  # too long for int(); no crash


def test_parse_simple_query() -> None:
    terms = parse("level:ERROR  timeout abc-123")
    assert terms == [
        FieldTerm("level", frozenset({field_key("level", "error")})),
        TextTerm(frozenset({text_key("timeout")})),
        TextTerm(frozenset({text_key("abc"), text_key("123")})),
    ]
    assert parse("") == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("level:", "expected field:value"),
        (":error", "expected field:value"),
        ("---", "no letters or digits"),
        ("msg:" + "x" * 300, "not indexed exactly"),
    ],
)
def test_bad_queries_say_why(text: str, message: str) -> None:
    with pytest.raises(QueryError, match=message):
        parse(text)


def test_matches_needs_every_term_and_any_value_of_a_field_term() -> None:
    event = terms_of({"level": "ERROR", "status": "503", "msg": "upstream timeout"})
    assert matches(event, parse("level:error status:503 timeout"))
    assert matches(event, parse("status:503.0"))  # "503" the string, as a number
    assert not matches(event, parse("level:error status:500"))
    assert not matches(event, parse("upstream refused"))
