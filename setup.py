from __future__ import annotations

import json
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class ActiveRegistryBuild(build_py):
    """Keep adapter history in source control without shipping it to every user."""

    def run(self) -> None:
        super().run()
        registry = Path(self.build_lib) / "agentweb" / "builtin_registry"
        index = json.loads((registry / "index.json").read_text(encoding="utf-8"))
        active = {
            (str(entry["name"]), str(entry["version"]))
            for entry in index.get("sites", [])
        }
        sites = registry / "sites"
        for site_dir in sites.iterdir():
            if not site_dir.is_dir():
                continue
            for version_dir in site_dir.iterdir():
                if version_dir.is_dir() and (site_dir.name, version_dir.name) not in active:
                    shutil.rmtree(version_dir)


setup(cmdclass={"build_py": ActiveRegistryBuild})
