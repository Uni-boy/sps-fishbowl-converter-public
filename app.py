"""Streamlit UI for the SPS Commerce to Fishbowl Sales Order converter."""

from __future__ import annotations

import csv
import tempfile
from decimal import Decimal
from pathlib import Path

import streamlit as st

from convert_sps_po_to_fishbowl_so import (
    ConversionError,
    build_output_rows,
    decimal_text,
    infer_date_format,
    load_template,
    parse_sps_order,
    validate_output_rows,
    write_csv,
)


APP_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = APP_DIR / "SalesOrder_template.csv"
def convert_upload(uploaded_bytes: bytes) -> tuple[bytes, object]:
    """Convert an uploaded SPS PO entirely in a temporary directory."""
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
        output_path = temp_path / "converted_sales_order.csv"
        po_path.write_bytes(uploaded_bytes)

        order = parse_sps_order(po_path, output_date_format)
        rows = build_output_rows(
            order,
            so_header,
            item_header,
            {},
            {},
            extra_so_defaults=private_defaults,
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
        st.warning("Please select an SPS Commerce PO CSV.")
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
