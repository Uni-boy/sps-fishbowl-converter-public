#!/usr/bin/env python3
"""Refresh the encrypted Part catalog from the Fishbowl read-only API."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from cryptography.fernet import Fernet, InvalidToken

from convert_sps_po_to_fishbowl_so import ConversionError, load_part_descriptions
from encrypt_parts import read_key


DEFAULT_BASE_URL = "http://xmyautoparts.myfishbowl.com:3404"
DEFAULT_APP_NAME = "XMY ReadOnly Probe"
DEFAULT_APP_DESCRIPTION = "Read-only inventory/order visibility"
DEFAULT_APP_ID = 48201
DEFAULT_PAGE_SIZE = 1000


class FishbowlSyncError(Exception):
    """A Fishbowl connection or catalog validation error."""


@dataclass(frozen=True)
class FishbowlConfig:
    base_url: str
    username: str
    password: str
    app_name: str = DEFAULT_APP_NAME
    app_description: str = DEFAULT_APP_DESCRIPTION
    app_id: int = DEFAULT_APP_ID


def load_env_file(path: Optional[Path]) -> None:
    """Load missing environment variables from a simple KEY=VALUE file."""
    if path is None:
        return
    if not path.is_file():
        raise FishbowlSyncError(f"Environment file not found: {path}")

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise FishbowlSyncError(
        "Missing required environment variable: " + " or ".join(names)
    )


def load_config() -> FishbowlConfig:
    return FishbowlConfig(
        base_url=os.environ.get("FISHBOWL_BASE_URL", os.environ.get("FB_BASE_URL", DEFAULT_BASE_URL)).rstrip("/"),
        username=require_env("FISHBOWL_USERNAME", "FB_USER"),
        password=require_env("FISHBOWL_PASSWORD", "FB_PASS"),
    )


def get_encryption_key(secrets_path: Optional[Path]) -> bytes:
    key = os.environ.get("PARTS_ENCRYPTION_KEY", "").strip()
    if key:
        return key.encode("ascii")
    if secrets_path is not None and secrets_path.is_file():
        return read_key(secrets_path)
    raise FishbowlSyncError(
        "PARTS_ENCRYPTION_KEY is missing and no readable secrets file was supplied"
    )


def response_json(response: requests.Response, label: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise FishbowlSyncError(f"{label} returned invalid JSON") from exc


def login(session: requests.Session, config: FishbowlConfig) -> str:
    response = session.post(
        f"{config.base_url}/api/login",
        json={
            "appName": config.app_name,
            "appDescription": config.app_description,
            "appId": config.app_id,
            "username": config.username,
            "password": config.password,
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response_json(response, "Fishbowl login")
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise FishbowlSyncError("Fishbowl login response did not contain a token")
    return token


def logout(
    session: requests.Session, config: FishbowlConfig, headers: dict[str, str]
) -> None:
    response = session.post(
        f"{config.base_url}/api/logout", headers=headers, timeout=30
    )
    response.raise_for_status()


def get_field(row: dict[str, Any], name: str) -> Any:
    for key, value in row.items():
        if key.casefold() == name.casefold():
            return value
    return None


def fetch_parts(
    session: requests.Session,
    config: FishbowlConfig,
    headers: dict[str, str],
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, str]:
    """Read every Fishbowl part in stable pages and validate the catalog."""
    descriptions: dict[str, str] = {}
    offset = 0

    while True:
        query = (
            "SELECT p.num AS PartNumber, p.description AS PartDescription "
            "FROM part p ORDER BY p.id "
            f"LIMIT {page_size} OFFSET {offset}"
        )
        response = session.get(
            f"{config.base_url}/api/data-query",
            headers=headers,
            params={"query": query},
            timeout=120,
        )
        response.raise_for_status()
        payload = response_json(response, "Fishbowl part query")
        if not isinstance(payload, list):
            raise FishbowlSyncError("Fishbowl part query did not return a row list")

        for row_number, row in enumerate(payload, start=offset + 1):
            if not isinstance(row, dict):
                raise FishbowlSyncError(
                    f"Fishbowl part row {row_number} is not an object"
                )
            raw_number = get_field(row, "PartNumber")
            raw_description = get_field(row, "PartDescription")
            part_number = "" if raw_number is None else str(raw_number).strip()
            description = (
                "" if raw_description is None else str(raw_description).strip()
            )
            if not part_number:
                raise FishbowlSyncError(
                    f"Fishbowl part row {row_number} has no PartNumber"
                )
            if not description:
                raise FishbowlSyncError(
                    f"Fishbowl part {part_number} has no PartDescription"
                )
            if part_number in descriptions:
                raise FishbowlSyncError(
                    f"Fishbowl returned duplicate PartNumber: {part_number}"
                )
            descriptions[part_number] = description

        if len(payload) < page_size:
            break
        offset += page_size

    if not descriptions:
        raise FishbowlSyncError("Fishbowl returned no parts")
    return descriptions


def read_encrypted_catalog(
    path: Path, key: bytes
) -> Optional[dict[str, str]]:
    if not path.is_file():
        return None
    try:
        decrypted = Fernet(key).decrypt(path.read_bytes())
    except (OSError, ValueError, InvalidToken) as exc:
        raise FishbowlSyncError(f"Cannot read existing encrypted catalog: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="fishbowl-parts-current-") as temp_dir:
        csv_path = Path(temp_dir) / "Part.csv"
        csv_path.write_bytes(decrypted)
        try:
            return load_part_descriptions(csv_path)
        except (ConversionError, OSError, csv.Error) as exc:
            raise FishbowlSyncError(
                f"Existing encrypted catalog is invalid: {exc}"
            ) from exc


def catalog_rows(descriptions: dict[str, str]) -> Iterable[tuple[str, str]]:
    for part_number in sorted(descriptions, key=str.casefold):
        yield part_number, descriptions[part_number]


def encrypt_catalog(
    descriptions: dict[str, str], output_path: Path, key: bytes
) -> None:
    with tempfile.TemporaryDirectory(prefix="fishbowl-parts-new-") as temp_dir:
        csv_path = Path(temp_dir) / "Part.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["PartNumber", "PartDescription"])
            writer.writerows(catalog_rows(descriptions))

        # Reuse the converter's strict loader before encrypting the new artifact.
        validated = load_part_descriptions(csv_path)
        if validated != descriptions:
            raise FishbowlSyncError("Generated Part.csv failed round-trip validation")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(Fernet(key).encrypt(csv_path.read_bytes()))


def summarize_changes(
    old: Optional[dict[str, str]], new: dict[str, str]
) -> tuple[int, int, int]:
    if old is None:
        return len(new), 0, 0
    added = new.keys() - old.keys()
    removed = old.keys() - new.keys()
    changed = {key for key in new.keys() & old.keys() if new[key] != old[key]}
    return len(added), len(removed), len(changed)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("data/parts.csv.fernet")
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--secrets-file", type=Path, default=Path(".streamlit/secrets.toml")
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Read and validate Fishbowl without modifying the encrypted catalog",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.page_size < 1 or args.page_size > 5000:
        print("ERROR: --page-size must be between 1 and 5000", file=sys.stderr)
        return 1

    try:
        load_env_file(args.env_file)
        config = load_config()
        key = get_encryption_key(args.secrets_file)
        current = read_encrypted_catalog(args.output, key)

        session = requests.Session()
        token: Optional[str] = None
        headers: dict[str, str] = {}
        primary_error: Optional[BaseException] = None
        try:
            token = login(session, config)
            headers = {"Authorization": f"Bearer {token}"}
            parts = fetch_parts(
                session, config, headers, page_size=args.page_size
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if token is not None:
                try:
                    logout(session, config, headers)
                except requests.RequestException as exc:
                    if primary_error is None:
                        raise FishbowlSyncError(
                            f"Fishbowl logout failed: {exc}"
                        ) from exc
                    print(f"WARNING: Fishbowl logout failed: {exc}", file=sys.stderr)
            session.close()

        added, removed, changed = summarize_changes(current, parts)
        print(f"Fishbowl parts: {len(parts)}")
        print(f"Added: {added}")
        print(f"Removed: {removed}")
        print(f"Description changed: {changed}")

        if current == parts:
            print("Catalog is already current; no file was changed.")
            return 0
        if args.check_only:
            print("Check-only mode; encrypted catalog was not changed.")
            return 0

        encrypt_catalog(parts, args.output, key)
        verified = read_encrypted_catalog(args.output, key)
        if verified != parts:
            raise FishbowlSyncError("Encrypted catalog failed final verification")
        print(f"Updated encrypted catalog: {args.output}")
        return 0
    except (
        FishbowlSyncError,
        ConversionError,
        OSError,
        UnicodeEncodeError,
        ValueError,
        requests.RequestException,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
