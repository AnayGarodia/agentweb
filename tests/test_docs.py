from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def test_local_documentation_links_exist() -> None:
    missing: list[str] = []
    documents = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
        *sorted((ROOT / "docs").glob("*.md")),
    ]

    for document in documents:
        for raw_target in MARKDOWN_LINK.findall(document.read_text()):
            target = raw_target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if not (document.parent / target).resolve().exists():
                missing.append(f"{document.relative_to(ROOT)} -> {raw_target}")

    assert missing == []


def test_readme_leads_with_a_copyable_install_and_real_call() -> None:
    readme = (ROOT / "README.md").read_text()

    install = (
        "curl -fsSL https://github.com/AnayGarodia/agentweb/"
        "raw/refs/heads/main/install.sh | sh"
    )
    example = "Use AgentWeb to find the latest version of React on npm."

    assert install in readme
    assert example in readme
    assert readme.index(install) < readme.index("## Just ask for what you want")
