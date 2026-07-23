from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path, PurePosixPath

PRIVATE_MODULES = {
    "agentweb/author_mcp.py",
    "agentweb/authoring.py",
    "agentweb/workbench.py",
}
PUBLIC_ADAPTERS = {"arxiv", "npm", "wikipedia"}
FORBIDDEN_PARTS = {
    "authoring",
    "captures",
    "browser-profiles",
    "verification.local.json",
}


def check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        leaked_modules = sorted(PRIVATE_MODULES.intersection(names))
        if leaked_modules:
            raise SystemExit(f"private factory modules in wheel: {leaked_modules}")

        leaked_paths = sorted(
            name
            for name in names
            if FORBIDDEN_PARTS.intersection(PurePosixPath(name).parts)
        )
        if leaked_paths:
            raise SystemExit(f"private local artifacts in wheel: {leaked_paths}")

        index = json.loads(wheel.read("agentweb/builtin_registry/index.json"))
        indexed = {str(entry["name"]) for entry in index.get("sites", [])}
        if indexed != PUBLIC_ADAPTERS:
            raise SystemExit(
                f"public registry must contain {sorted(PUBLIC_ADAPTERS)}, got {sorted(indexed)}"
            )

        bundled = {
            PurePosixPath(name).parts[3]
            for name in names
            if name.startswith("agentweb/builtin_registry/sites/")
            and name.endswith("/manifest.json")
        }
        if bundled != PUBLIC_ADAPTERS:
            raise SystemExit(
                f"wheel adapter bundles must contain {sorted(PUBLIC_ADAPTERS)}, got {sorted(bundled)}"
            )

        source_modules = {
            PurePosixPath(name).name
            for name in names
            if name.startswith("agentweb/") and name.endswith(".py")
        }
        required = {"cli.py", "runtime.py", "sdk.py", "registry.py", "storage.py"}
        if not required.issubset(source_modules):
            missing = sorted(required - source_modules)
            raise SystemExit(f"public core modules missing from wheel: {missing}")

    print(f"public release boundary passed: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    check_wheel(args.wheel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
