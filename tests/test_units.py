from __future__ import annotations

import logging

import pytest

from agentweb import logs
from agentweb.sdk import (
    AgentWebError,
    bounded_data,
    enforce_data_budget,
    parse_query_pairs,
    parse_xml_feed,
    redact_sensitive_values,
)
from agentweb.targets import (
    canonical_domain,
    extract_resource,
    host_matches,
    normalized_host,
    target_url,
)


def test_parse_query_pairs_from_list_and_dict():
    assert parse_query_pairs(["a=1", "b=2"]) == {"a": "1", "b": "2"}
    assert parse_query_pairs({"a": 1}) == {"a": 1}
    assert parse_query_pairs(None) == {}


def test_parse_query_pairs_accepts_bracketed_rails_names():
    assert parse_query_pairs(["conditions[term]=climate"]) == {
        "conditions[term]": "climate"
    }
    with pytest.raises(AgentWebError):
        parse_query_pairs(["bad name!=1"])


def test_parse_query_pairs_repeated_key_becomes_list():
    assert parse_query_pairs(["a=1", "a=2", "a=3"]) == {"a": ["1", "2", "3"]}


def test_parse_query_pairs_rejects_bad_input():
    with pytest.raises(AgentWebError):
        parse_query_pairs(["novalue"])
    with pytest.raises(AgentWebError):
        parse_query_pairs(["=1"])


def test_bounded_data_truncates_long_strings_and_lists():
    value, truncated = bounded_data("x" * 100, max_string=10)
    assert truncated is True
    assert value.endswith("…")
    trimmed, truncated = bounded_data(list(range(100)), max_items=5)
    assert truncated is True
    assert len(trimmed) == 5


def test_bounded_data_leaves_small_values_untouched():
    value, truncated = bounded_data({"a": [1, 2], "b": "hi"})
    assert truncated is False
    assert value == {"a": [1, 2], "b": "hi"}


def test_enforce_data_budget_preview_on_overflow():
    payload = {"items": ["x" * 50 for _ in range(200)]}
    result, truncated, original = enforce_data_budget(payload, max_total_chars=500)
    assert truncated is True
    assert original > 500
    assert result["format"] == "truncated_json_preview"


def test_enforce_data_budget_passthrough_when_small():
    result, truncated, original = enforce_data_budget({"a": 1})
    assert truncated is False
    assert result == {"a": 1}
    assert original > 0


def test_redact_sensitive_values_matches_by_type_and_value():
    payload = {"token": "secret", "keep": "secret-ish", "n": 5}
    redacted = redact_sensitive_values(payload, ["secret", "", None])
    assert redacted["token"] == "[redacted]"
    assert redacted["keep"] == "secret-ish"
    assert redacted["n"] == 5


def test_parse_xml_feed_atom_and_rss():
    atom = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Example</title>
      <entry><title>First</title><link href="https://example.com/1"/></entry>
    </feed>"""
    parsed = parse_xml_feed(atom)
    assert parsed["title"] == "Example"
    assert parsed["entries"][0]["title"] == "First"

    rss = b"""<?xml version="1.0"?>
    <rss><channel><title>Chan</title>
      <item><title>Post</title></item>
    </channel></rss>"""
    parsed = parse_xml_feed(rss)
    assert parsed["title"] == "Chan"
    assert parsed["entries"][0]["title"] == "Post"


def test_parse_xml_feed_rejects_doctype_and_entities():
    with pytest.raises(AgentWebError) as excinfo:
        parse_xml_feed(b"<!DOCTYPE feed><feed></feed>")
    assert excinfo.value.as_dict()["error"] == "unsafe_xml_feed"


def test_parse_xml_feed_reports_malformed():
    with pytest.raises(AgentWebError) as excinfo:
        parse_xml_feed(b"<feed><unclosed>")
    assert excinfo.value.as_dict()["error"] == "invalid_xml_feed"


def test_normalized_and_canonical_host():
    assert normalized_host("HTTPS://Example.com/path") == "example.com"
    assert canonical_domain({"base_url": "https://www.npmjs.com"}) == "npmjs.com"
    assert canonical_domain({"canonical_domain": "GitHub.com"}) == "github.com"


def test_target_url_validation():
    assert target_url("plain-name") is None
    assert target_url("https://example.com/x") == "https://example.com/x"
    with pytest.raises(AgentWebError):
        target_url("ftp://example.com")


def test_host_matches_covers_subdomains():
    assert host_matches("api.example.com", "example.com")
    assert host_matches("example.com", ".example.com")
    assert not host_matches("evilexample.com", "example.com")


def test_extract_resource_applies_routes_with_query_and_transform():
    manifest = {
        "url_routes": [
            {
                "operation": "package_get",
                "host_regex": r"(www\.)?npmjs\.com",
                "path_regex": r"/package/(?P<name>[^/]+)",
                "arguments": {"name": {"group": "name"}},
            },
            {
                "operation": "search",
                "host_regex": r".*",
                "path_regex": r"/search",
                "arguments": {
                    "q": {"query": "q"},
                    "page": {"query": "page", "transform": "integer"},
                },
            },
        ]
    }
    op, args = extract_resource(manifest, "https://npmjs.com/package/react")
    assert op == "package_get"
    assert args == {"name": "react"}

    op, args = extract_resource(
        manifest, "https://npmjs.com/search?q=hello&page=3"
    )
    assert op == "search"
    assert args == {"q": "hello", "page": 3}


def test_extract_resource_skips_route_when_required_query_missing():
    manifest = {
        "url_routes": [
            {
                "operation": "search",
                "path_regex": r"/search",
                "arguments": {"q": {"query": "q"}},
            }
        ]
    }
    assert extract_resource(manifest, "https://example.com/search") is None


def test_configure_logging_is_idempotent(monkeypatch):
    monkeypatch.setenv("AGENTWEB_DEBUG", "1")
    logs.logger.handlers = [logging.NullHandler()]
    logs.configure_logging()
    logs.configure_logging()
    debug_handlers = [
        h
        for h in logs.logger.handlers
        if getattr(h, "name", None) == "agentweb-debug-stderr"
    ]
    assert len(debug_handlers) == 1
    assert logs.debug_enabled() is True


def test_configure_logging_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("AGENTWEB_DEBUG", raising=False)
    logs.logger.handlers = [logging.NullHandler()]
    logs.configure_logging()
    assert logs.debug_enabled() is False
    assert all(
        getattr(h, "name", None) != "agentweb-debug-stderr"
        for h in logs.logger.handlers
    )
