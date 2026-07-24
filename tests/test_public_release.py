from pathlib import Path


def test_private_factory_is_not_in_public_source_tree() -> None:
    root = Path(__file__).parents[1]
    private = [
        root / "src/agentweb/author_mcp.py",
        root / "src/agentweb/authoring.py",
        root / "src/agentweb/workbench.py",
        root / "scripts/author_npm_adapter.py",
        root / "authoring",
    ]

    assert not [path for path in private if path.exists()]


def test_only_reference_adapters_are_bundled() -> None:
    sites = Path(__file__).parents[1] / "src/agentweb/builtin_registry/sites"
    assert {path.name for path in sites.iterdir() if path.is_dir()} == {
        "amazon",
        "arxiv",
        "github",
        "gst",
        "hn",
        "huggingface",
        "linkedin",
        "npm",
        "pypi",
        "spotify",
        "stackoverflow",
        "wikipedia",
    }
