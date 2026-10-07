from logscope.terms import (
    MAX_TOKEN,
    exists_key,
    field_key,
    query_values,
    terms_of,
    word_key,
    word_lists,
)


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
        assert word_key(word) in terms


def test_timestamp_fields_are_not_indexed() -> None:
    terms = terms_of({"time": "2026-10-06T21:00:00Z", "ts": 1, "msg": "hi"})
    assert terms == {field_key("msg", "hi"), word_key("hi"), exists_key("msg")}


def test_long_strings_are_words_only_and_long_words_are_cut() -> None:
    long_value = "word " * 100
    terms = terms_of({"msg": long_value, "blob": "x" * 100})
    assert not any(t.startswith("f\0msg\0") for t in terms)
    assert word_key("word") in terms
    assert word_key("x" * MAX_TOKEN) in terms


def test_query_values_match_a_string_and_its_number() -> None:
    assert query_values("7.0") == {"7.0", "7"}
    assert query_values("007") == {"007"}  # not JSON number syntax: a string
    assert query_values("2e2") == {"2e2", "200"}
    assert query_values("ERROR") == {"error"}
    assert query_values("1" * 5000) == {"1" * 5000}  # too long for int(); no crash


def test_a_field_and_every_enclosing_object_exist() -> None:
    terms = terms_of({"http": {"req": {"method": "GET"}}, "tags": [], "user": None})
    assert {
        exists_key("http"),
        exists_key("http.req"),
        exists_key("http.req.method"),
    } <= terms
    assert exists_key("user") in terms  # null is present
    assert exists_key("tags") not in terms  # an empty array holds nothing


def test_word_lists_keep_each_value_s_words_in_order() -> None:
    lists = word_lists({"msg": "Upstream timeout", "status": 504, "ok": True})
    assert sorted(lists) == [["504"], ["upstream", "timeout"]]


def test_a_string_that_is_a_number_also_gets_the_number_s_key() -> None:
    terms = terms_of({"status": "503.0", "code": "007"})
    assert {field_key("status", "503.0"), field_key("status", "503")} <= terms
    assert not any(
        t.startswith("f\0code\0") and t != field_key("code", "007") for t in terms
    )
