from __future__ import annotations

import os

import pytest

from agentweb.runtime import Runtime
from agentweb.storage import StatePaths

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTWEB_LIVE_TESTS") != "1",
    reason="set AGENTWEB_LIVE_TESTS=1 to call public production APIs",
)


def runtime_for(tmp_path) -> Runtime:
    runtime = Runtime(StatePaths(tmp_path), fresh=True)
    runtime.registry.sync()
    return runtime


def test_live_wikipedia(tmp_path) -> None:
    runtime = runtime_for(tmp_path)
    search = runtime.call(
        "wikipedia.search", {"query": "Alan Turing", "limit": 3, "language": "en"}
    )
    page = runtime.call(
        "wikipedia.page",
        {"title": search["results"][0]["title"], "language": "en", "max_chars": 1000},
    )

    assert search["count"] == 3
    assert page["page"]["extract"]
    assert len(page["page"]["extract"]) <= 1000


def test_live_arxiv(tmp_path) -> None:
    runtime = runtime_for(tmp_path)
    result = runtime.call(
        "arxiv.search_papers", {"query": "attention", "limit": 2}
    )

    assert result["count"] == 2
    assert all(paper["id"] for paper in result["papers"])


def test_live_npm(tmp_path) -> None:
    runtime = runtime_for(tmp_path)
    result = runtime.call("npm.get_package", {"package": "react"})

    assert result["package"]["name"] == "react"
    assert result["package"]["latest_version"]
