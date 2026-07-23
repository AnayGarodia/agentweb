from __future__ import annotations

import argparse
import base64
import hashlib
import json
import zipfile
from pathlib import Path


def validate_active_registry(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as wheel:
        index = json.loads(wheel.read("agentweb/builtin_registry/index.json"))
        active = {
            f"agentweb/builtin_registry/sites/{entry['name']}/{entry['version']}"
            for entry in index.get("sites", [])
        }
        bundled = {
            str(Path(name).parent)
            for name in wheel.namelist()
            if name.startswith("agentweb/builtin_registry/sites/")
            and name.endswith("/manifest.json")
        }
    if bundled != active:
        extra = sorted(bundled - active)
        missing = sorted(active - bundled)
        raise SystemExit(
            "Wheel registry does not match index.json "
            f"(extra={extra}, missing={missing})"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    validate_active_registry(args.wheel)
    wheel = args.wheel.read_bytes()
    wheel_sha256 = hashlib.sha256(wheel).hexdigest()
    rendered = (
        args.template.read_text(encoding="utf-8")
        .replace("__VERSION__", args.version)
        .replace("__SHA256__", wheel_sha256)
        .replace("__WHEEL_BASE64__", base64.b64encode(wheel).decode("ascii"))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    args.output.chmod(0o755)
    args.wheel.with_name(args.wheel.name + ".sha256").write_text(
        f"{wheel_sha256}  {args.wheel.name}\n", encoding="utf-8"
    )
    installer_sha256 = hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_name(args.output.name + ".sha256").write_text(
        f"{installer_sha256}  {args.output.name}\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
