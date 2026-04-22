#!/usr/bin/env python3
"""PostToolUse hook: regenerate .env.example from .env, blanking secret values.

Reads hook JSON from stdin. If the edited file is .env (not .env.example),
rewrite .env.example by copying .env line-by-line and blanking values for
secret-like keys.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SECRET_KEY_PATTERNS = [
    re.compile(r"^KIS_APP_KEY$"),
    re.compile(r"^KIS_APP_SECRET$"),
    re.compile(r"^KIS_ACCOUNT_NO$"),
    re.compile(r"^NAVER_CLIENT_ID$"),
    re.compile(r"^NAVER_CLIENT_SECRET$"),
    re.compile(r"^ANTHROPIC_API_KEY$"),
    re.compile(r".*TOKEN.*"),
    re.compile(r".*SECRET.*"),
    re.compile(r".*API_KEY.*"),
]


def is_secret_key(key: str) -> bool:
    return any(p.match(key) for p in SECRET_KEY_PATTERNS)


def transform(env_text: str) -> str:
    out_lines = []
    for line in env_text.splitlines():
        stripped = line.lstrip()
        # Preserve blanks and comments as-is
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$", line)
        if not m:
            out_lines.append(line)
            continue
        indent, key, eq, _value = m.groups()
        if is_secret_key(key):
            out_lines.append(f"{indent}{key}=")
        else:
            out_lines.append(line)
    # Preserve trailing newline if original had one
    suffix = "\n" if env_text.endswith("\n") else ""
    return "\n".join(out_lines) + suffix


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return 0

    # Normalize path separators
    fp = file_path.replace("\\", "/")
    base = os.path.basename(fp)
    # Only react to .env (not .env.example, not .env.something)
    if base != ".env":
        return 0

    env_path = Path(file_path)
    example_path = env_path.with_name(".env.example")

    try:
        text = env_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"sync-env-example: cannot read {env_path}: {e}", file=sys.stderr)
        return 0

    new_example = transform(text)
    try:
        example_path.write_text(new_example, encoding="utf-8")
    except Exception as e:
        print(f"sync-env-example: cannot write {example_path}: {e}", file=sys.stderr)
        return 0

    print(json.dumps({"systemMessage": f"Synced {example_path.name} from .env (secrets blanked)"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
