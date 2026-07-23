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
        ROOT / "skills" / "agentweb" / "SKILL.md",
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
    assert "Browserless website automation for coding agents" in readme
    assert readme.index(install) < readme.index("## Just ask for what you want")


def test_agentweb_skill_is_discoverable_and_actionable() -> None:
    skill = ROOT / "skills" / "agentweb" / "SKILL.md"
    text = skill.read_text()

    assert text.startswith("---\nname: agentweb\n")
    assert "browserless, typed website actions" in text
    assert "when a task mentions website automation" in text
    assert "command -v agentweb" in text
    assert '$HOME/.local/bin/agentweb' in text
    assert '"$AGENTWEB_BIN" capabilities DOMAIN --query WORD' in text
    assert '"$AGENTWEB_BIN" connect DOMAIN --mode login' in text
    assert "authentication_required" in text
    assert "ask whether the\nuser wants to log in or sign up" in text
    assert "preserves a portable skill installed by GitHub CLI" in text
    assert "confirmation flag only for that approved action" in text


def test_readme_exposes_github_agent_skill_discovery() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "gh skill search agentweb" in readme
    assert "gh skill preview AnayGarodia/agentweb agentweb" in readme
    assert (
        "gh skill install AnayGarodia/agentweb agentweb "
        "--agent codex --scope user"
    ) in readme


def test_generated_installer_matches_its_template() -> None:
    installer = (ROOT / "install.sh").read_text()
    installer = re.sub(
        r'^VERSION="[^"]+"$', 'VERSION="__VERSION__"', installer, flags=re.MULTILINE
    )
    installer = re.sub(
        r'^EXPECTED_SHA256="[^"]+"$',
        'EXPECTED_SHA256="__SHA256__"',
        installer,
        flags=re.MULTILINE,
    )
    installer = re.sub(
        r'^payload = """.*"""$',
        'payload = """__WHEEL_BASE64__"""',
        installer,
        flags=re.MULTILINE,
    )

    assert installer == (ROOT / "installer" / "install.sh.template").read_text()
