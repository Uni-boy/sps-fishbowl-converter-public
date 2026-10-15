"""Streamlit UI for the SPS Commerce to Fishbowl Sales Order converter."""

from __future__ import annotations

import csv
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import streamlit as st
from cryptography.fernet import Fernet, InvalidToken
from requests import RequestException

from convert_sps_po_to_fishbowl_so import (
    ConversionError,
    build_output_rows,
    decimal_text,
    infer_date_format,
    load_part_descriptions,
    load_template,
    parse_sps_order,
    validate_output_rows,
    write_csv,
)
from sync_parts_from_fishbowl import (
    DEFAULT_APP_DESCRIPTION,
    DEFAULT_APP_ID,
    DEFAULT_APP_NAME,
    DEFAULT_BASE_URL,
    FishbowlConfig,
    FishbowlSyncError,
    encrypt_catalog,
    fetch_catalog_from_fishbowl,
    read_encrypted_catalog,
    summarize_changes,
)


APP_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = APP_DIR / "SalesOrder_template.csv"
ENCRYPTED_PARTS_PATH = APP_DIR / "data" / "parts.csv.fernet"
RUNTIME_ENCRYPTED_PARTS_PATH = (
    Path(tempfile.gettempdir()) / "sps-fishbowl-runtime-parts.csv.fernet"
)


def part_encryption_key() -> bytes:
    if "PARTS_ENCRYPTION_KEY" not in st.secrets:
        raise ConversionError(
            "The server is missing PARTS_ENCRYPTION_KEY in Streamlit Secrets."
        )
    try:
        return str(st.secrets["PARTS_ENCRYPTION_KEY"]).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConversionError("The Part catalog encryption key is invalid.") from exc


def active_encrypted_parts_path() -> Path:
    if RUNTIME_ENCRYPTED_PARTS_PATH.is_file():
        return RUNTIME_ENCRYPTED_PARTS_PATH
    return ENCRYPTED_PARTS_PATH


def decrypt_part_catalog(destination: Path) -> None:
    """Decrypt the bundled Part catalog using a Streamlit Secret."""
    encrypted_path = active_encrypted_parts_path()
    if not encrypted_path.is_file():
        raise ConversionError("The encrypted Part catalog is not deployed.")

    try:
        decrypted = Fernet(part_encryption_key()).decrypt(
            encrypted_path.read_bytes()
        )
    except (ValueError, InvalidToken, UnicodeEncodeError) as exc:
        raise ConversionError(
            "The encrypted Part catalog or its server key is invalid."
        ) from exc

    destination.write_bytes(decrypted)


def require_streamlit_secret(name: str) -> str:
    if name not in st.secrets or not str(st.secrets[name]).strip():
        raise FishbowlSyncError(
            f"The server is missing {name} in Streamlit Secrets."
        )
    return str(st.secrets[name]).strip()


def fishbowl_config_from_secrets() -> FishbowlConfig:
    base_url = str(st.secrets.get("FISHBOWL_BASE_URL", DEFAULT_BASE_URL)).rstrip(
        "/"
    )
    return FishbowlConfig(
        base_url=base_url,
        username=require_streamlit_secret("FISHBOWL_USERNAME"),
        password=require_streamlit_secret("FISHBOWL_PASSWORD"),
        app_name=DEFAULT_APP_NAME,
        app_description=DEFAULT_APP_DESCRIPTION,
        app_id=DEFAULT_APP_ID,
    )


def update_runtime_part_catalog() -> dict[str, Any]:
    """Synchronize Fishbowl parts into the running app's encrypted catalog."""
    key = part_encryption_key()
    current = read_encrypted_catalog(active_encrypted_parts_path(), key)
    parts = fetch_catalog_from_fishbowl(fishbowl_config_from_secrets())
    added, removed, changed = summarize_changes(current, parts)

    updated = current != parts
    if updated:
        candidate = RUNTIME_ENCRYPTED_PARTS_PATH.with_suffix(".tmp")
        try:
            encrypt_catalog(parts, candidate, key)
            verified = read_encrypted_catalog(candidate, key)
            if verified != parts:
                raise FishbowlSyncError(
                    "The refreshed encrypted Part catalog failed validation."
                )
            candidate.replace(RUNTIME_ENCRYPTED_PARTS_PATH)
        finally:
            candidate.unlink(missing_ok=True)

    return {
        "count": len(parts),
        "added": added,
        "removed": removed,
        "changed": changed,
        "updated": updated,
    }


def convert_upload(uploaded_bytes: bytes) -> tuple[bytes, object]:
    """Convert one uploaded SPS PO using the encrypted server catalog."""
    so_header, item_header = load_template(TEMPLATE_PATH)
    output_date_format = infer_date_format([], so_header)
    private_defaults = {}
    if "fishbowl_defaults" in st.secrets:
        private_defaults = {
            key: str(value)
            for key, value in st.secrets["fishbowl_defaults"].items()
        }

    with tempfile.TemporaryDirectory(prefix="sps-fishbowl-") as temp_dir:
        temp_path = Path(temp_dir)
        po_path = temp_path / "uploaded_po.csv"
        parts_path = temp_path / "decrypted_parts.csv"
        output_path = temp_path / "converted_sales_order.csv"
        po_path.write_bytes(uploaded_bytes)
        decrypt_part_catalog(parts_path)

        order = parse_sps_order(po_path, output_date_format)
        part_descriptions = load_part_descriptions(parts_path)
        rows = build_output_rows(
            order,
            so_header,
            item_header,
            {},
            {},
            extra_so_defaults=private_defaults,
            part_descriptions=part_descriptions,
        )
        validate_output_rows(rows, so_header, item_header, len(order.items))
        write_csv(output_path, rows)

        # Verify the serialized artifact before returning it to the browser.
        with output_path.open("r", encoding="utf-8", newline="") as handle:
            written_rows = list(csv.reader(handle))
        validate_output_rows(
            written_rows, so_header, item_header, len(order.items)
        )
        return output_path.read_bytes(), order


st.set_page_config(
    page_title="SPS → Fishbowl Sales Order",
    page_icon="📦",
    layout="centered",
)

st.title("SPS → Fishbowl Sales Order")
st.write(
    "Upload an SPS Commerce PO CSV. The app validates the order and creates a "
    "Fishbowl-ready Sales Order CSV."
)

st.subheader("Part catalog")
st.caption(
    "Update reads the current PartNumber and description list directly from "
    "Fishbowl using the approved read-only connection."
)
if st.button(
    "Update Part catalog",
    type="secondary",
    use_container_width=True,
):
    try:
        with st.spinner("Reading and validating Fishbowl parts..."):
            result = update_runtime_part_catalog()
        if result["updated"]:
            st.success(
                "Part catalog updated. "
                f"{result['count']:,} parts; "
                f"{result['added']} added, "
                f"{result['removed']} removed, "
                f"{result['changed']} descriptions changed."
            )
        else:
            st.success(
                f"Part catalog is already current ({result['count']:,} parts)."
            )
    except (
        ConversionError,
        FishbowlSyncError,
        OSError,
        csv.Error,
        RequestException,
        ValueError,
    ) as exc:
        st.error(f"Part catalog update stopped: {exc}")

with st.form("converter"):
    uploaded_file = st.file_uploader(
        "SPS Commerce PO CSV",
        type=["csv"],
        help="Upload one SPS purchase order at a time.",
    )
    submitted = st.form_submit_button(
        "Convert and validate", type="primary", use_container_width=True
    )

if submitted:
    if uploaded_file is None:
        st.warning("Please select an SPS PO CSV.")
    else:
        try:
            output_bytes, order = convert_upload(uploaded_file.getvalue())
            quantity_total = sum(
                (item.quantity for item in order.items), Decimal("0")
            )
            calculated_total = sum(
                (item.extended_amount for item in order.items), Decimal("0")
            )

            st.success("Conversion and validation passed.")
            col1, col2, col3 = st.columns(3)
            col1.metric("Item rows", len(order.items))
            col2.metric("Total quantity", decimal_text(quantity_total))
            col3.metric("Order total", f"${calculated_total:,.2f}")
            st.caption(
                f"Part description mapping: {len(order.items)}/"
                f"{len(order.items)} matched"
            )

            st.subheader("Order summary")
            st.write(f"**PO Number:** {order.po_number}")
            st.write(f"**PO Date:** {order.po_date}")
            st.write(f"**Scheduled ship date:** {order.ship_date}")
            st.write(
                f"**Requested delivery date:** {order.requested_delivery_date}"
            )
            st.write(
                "**Ship To:** "
                f"{order.ship_to['ShipToName']}, "
                f"{order.ship_to['ShipToAddress']}, "
                f"{order.ship_to['ShipToCity']}, "
                f"{order.ship_to['ShipToState']} "
                f"{order.ship_to['ShipToZip']}"
            )

            for warning in order.warnings:
                st.warning(warning)

            st.download_button(
                "Download Fishbowl Sales Order CSV",
                data=output_bytes,
                file_name=f"Converted_SalesOrder_{order.po_number}.csv",
                mime="text/csv",
                type="primary",
                use_container_width=True,
            )
        except (ConversionError, OSError, csv.Error) as exc:
            st.error(f"Conversion stopped: {exc}")

st.caption(
    "Files are processed in a temporary directory and are not intentionally "
    "stored by this application."
)
