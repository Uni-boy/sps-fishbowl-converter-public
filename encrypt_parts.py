"""Encrypt a private Fishbowl Part.csv for bundled Streamlit deployment."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from cryptography.fernet import Fernet


KEY_PATTERN = re.compile(
    r'^\s*PARTS_ENCRYPTION_KEY\s*=\s*"([^"]+)"\s*$',
    re.MULTILINE,
)


def read_key(secrets_path: Path) -> bytes:
    """Read PARTS_ENCRYPTION_KEY without requiring a TOML dependency."""
    try:
        secrets_text = secrets_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Cannot read secrets file: {exc}") from exc

    match = KEY_PATTERN.search(secrets_text)
    if not match:
        raise SystemExit(
            f"PARTS_ENCRYPTION_KEY is missing from {secrets_path}"
        )
    return match.group(1).encode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Encrypt Part.csv for the Streamlit application."
    )
    parser.add_argument("--input", type=Path, default=Path("Part.csv"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/parts.csv.fernet")
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        default=Path(".streamlit/secrets.toml"),
    )
    args = parser.parse_args()

    key = read_key(args.secrets)
    try:
        encrypted = Fernet(key).encrypt(args.input.read_bytes())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot encrypt Part catalog: {exc}") from exc

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encrypted)
    print(f"Encrypted {args.input} -> {args.output}")


if __name__ == "__main__":
    main()
