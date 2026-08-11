from __future__ import annotations

import argparse
import re
from pathlib import Path

KEY_VALUE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def read_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = KEY_VALUE.match(raw.strip())
        if match:
            values[match.group(1)] = match.group(2)
    return values


def merge_env(target: Path, values: dict[str, str]) -> None:
    original = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    written: set[str] = set()
    output: list[str] = []
    for line in original:
        match = KEY_VALUE.match(line.strip())
        if match and match.group(1) in values:
            key = match.group(1)
            output.append(f"{key}={values[key]}")
            written.add(key)
        else:
            output.append(line)
    missing = [key for key in values if key not in written]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Applied STT benchmark selection")
        output.extend(f"{key}={values[key]}" for key in missing)
    target.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge benchmark/selected_stt.env into .env")
    parser.add_argument("--selection", type=Path, default=Path("benchmark/selected_stt.env"))
    parser.add_argument("--target", type=Path, default=Path(".env"))
    args = parser.parse_args()
    values = read_values(args.selection)
    if not values.get("STT_MODE"):
        raise RuntimeError("selection file does not contain STT_MODE")
    merge_env(args.target, values)
    print(f"applied {args.selection} -> {args.target}: {', '.join(values)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
