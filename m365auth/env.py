"""Loading, writing, and naming of `.env` credential values."""

from __future__ import annotations

import os
import re
from pathlib import Path


def env_name(*parts: str) -> str:
    joined = "_".join(parts)
    return re.sub(r"[^A-Z0-9]+", "_", joined.upper()).strip("_")


def header_env_name(host: str, header_name: str) -> str:
    return env_name(host, header_name)


def indexed_env_name(index: int, field_name: str) -> str:
    return env_name("HAR_ENTRY", str(index), field_name)


def dotenv_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return f'"{escaped}"'


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key] = value

    return {**values, **os.environ}


def set_env_values(path: Path, updates: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    rewritten: list[str] = []

    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rewritten.append(line)
            continue

        key = line.split("=", 1)[0].strip()
        if key in updates:
            rewritten.append(f"{key}={dotenv_quote(updates[key])}")
            seen.add(key)
        else:
            rewritten.append(line)

    for key, value in updates.items():
        if key not in seen:
            rewritten.append(f"{key}={dotenv_quote(value)}")

    path.write_text("\n".join(rewritten).rstrip() + "\n", encoding="utf-8")


def update_gitignore(path: Path) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    wanted = [".env", "*.env.local", "__pycache__/", "*.py[cod]"]
    changed = False
    for item in wanted:
        if item not in existing:
            existing.append(item)
            changed = True
    if changed:
        path.write_text("\n".join(existing).rstrip() + "\n", encoding="utf-8")
