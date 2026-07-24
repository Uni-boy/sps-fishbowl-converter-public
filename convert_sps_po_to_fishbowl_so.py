#!/usr/bin/env python3
"""Convert an SPS Commerce purchase-order CSV to a Fishbowl Sales Order CSV."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence


class ConversionError(Exception):
    """A conversion or validation error that should stop the import."""


UOM_MAP = {
    "each": "ea",
    "ea": "ea",
}

COUNTRY_MAP = {
    "US": "UNITED STATES",
    "USA": "UNITED STATES",
    "UNITED STATES": "UNITED STATES",
    "UNITED STATES OF AMERICA": "UNITED STATES",
}

SO_DEFAULTS = {
    "Flag": "SO",
    "SONum": "",
    # Fishbowl permits only Estimate (10), Issued (20), or Historical (95).
    "Status": "20",
    "ShipToResidential": "false",
}

ITEM_DEFAULTS = {
    "Flag": "Item",
    "SONum": "",
    "SOItemTypeID": "10",
    "Taxable": "true",
    "TaxCode": "NON",
    "ItemQuickBooksClassName": "None",
    "ShowItem": "true",
    "KitItem": "false",
}

REQUIRED_SPS_HEADERS = {
    "PO Number",
    "PO Date",
    "Requested Delivery Date",
    "Ship Dates",
    "Qty Ordered",
    "Unit Price",
    "Buyers Catalog or Stock Keeping #",
    "Record Type",
}


@dataclass(frozen=True)
class SPSItem:
    line_number: str
    product_number: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    uom: str

    @property
    def extended_amount(self) -> Decimal:
        return self.quantity * self.unit_price


@dataclass(frozen=True)
class SPSOrder:
    po_number: str
    po_date: str
    ship_date: str
    requested_delivery_date: str
    po_total: Decimal
    ship_to: dict[str, str]
    items: list[SPSItem]
    warnings: list[str]


def read_csv_rows(path: Path) -> list[list[str]]:
    if not path.is_file():
        raise ConversionError(f"File not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def normalized_header(row: Sequence[str]) -> list[str]:
    return [cell.strip() for cell in row]


def find_header_row(
    rows: Sequence[Sequence[str]], required: set[str], label: str
) -> tuple[int, list[str]]:
    for index, row in enumerate(rows):
        header = normalized_header(row)
        if required.issubset(set(header)):
            return index, header
    missing_text = ", ".join(sorted(required))
    raise ConversionError(f"Could not find {label} header containing: {missing_text}")


def row_as_dict(header: Sequence[str], row: Sequence[str]) -> dict[str, str]:
    padded = list(row) + [""] * max(0, len(header) - len(row))
    return {name: padded[index].strip() for index, name in enumerate(header) if name}


def require(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ConversionError(f"Missing required value: {label}")
    return value


def parse_decimal(value: str, label: str) -> Decimal:
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        raise ConversionError(f"Missing required numeric value: {label}")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ConversionError(f"Invalid numeric value for {label}: {value!r}") from exc


def parse_date(value: str, label: str, output_format: str) -> str:
    value = require(value, label)
    formats = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y%m%d")
    for date_format in formats:
        try:
            return datetime.strptime(value, date_format).strftime(output_format)
        except ValueError:
            pass
    raise ConversionError(f"Unable to parse {label}: {value!r}")


def infer_date_format(reference_rows: Sequence[Sequence[str]], so_header: Sequence[str]) -> str:
    date_index = so_header.index("Date")
    for row in reference_rows:
        if row and row[0].strip().upper() == "SO" and len(row) > date_index:
            sample = row[date_index].strip()
            for date_format in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
                try:
                    datetime.strptime(sample, date_format)
                    return date_format
                except ValueError:
                    pass
    return "%m/%d/%Y"


def load_template(path: Path) -> tuple[list[str], list[str]]:
    rows = read_csv_rows(path)
    _, so_header = find_header_row(
        rows, {"Flag", "SONum", "PONum", "OrderDateScheduled"}, "Fishbowl SO"
    )
    _, item_header = find_header_row(
        rows,
        {"Flag", "ProductNumber", "ProductQuantity", "ProductPrice", "UOM"},
        "Fishbowl Item",
    )
    if so_header == item_header:
        raise ConversionError("Template must contain distinct SO and Item header rows")
    return so_header, item_header


def load_part_descriptions(path: Path) -> dict[str, str]:
    rows = read_csv_rows(path)
    header_index, header = find_header_row(
        rows, {"PartNumber", "PartDescription"}, "Fishbowl Part"
    )
    descriptions: dict[str, str] = {}
    for position, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(cell.strip() for cell in row):
            continue
        record = row_as_dict(header, row)
        part_number = require(
            record.get("PartNumber", ""), f"PartNumber on Part.csv line {position}"
        )
        description = require(
            record.get("PartDescription", ""),
            f"PartDescription for PartNumber {part_number}",
        )
        if part_number in descriptions:
            raise ConversionError(
                f"Duplicate PartNumber in Part.csv: {part_number}"
            )
        descriptions[part_number] = description
    if not descriptions:
        raise ConversionError("Part.csv contains no part records")
    return descriptions


def load_reference_defaults(
    path: Path, so_header: Sequence[str], item_header: Sequence[str]
) -> tuple[dict[str, str], dict[str, str], list[list[str]]]:
    rows = read_csv_rows(path)
    so_row = next((row for row in rows if row and row[0].strip().upper() == "SO"), None)
    item_row = next(
        (row for row in rows if row and row[0].strip().upper() == "ITEM"), None
    )
    if so_row is None or item_row is None:
        raise ConversionError("Reference CSV must contain at least one SO and one Item row")
    return (
        row_as_dict(so_header, so_row),
        row_as_dict(item_header, item_row),
        rows,
    )


def map_uom(raw_uom: str, line_label: str) -> str:
    key = require(raw_uom, f"UOM on {line_label}").casefold()
    if key not in UOM_MAP:
        raise ConversionError(
            f"Unrecognized UOM {raw_uom!r} on {line_label}; add it to UOM_MAP"
        )
    return UOM_MAP[key]


def parse_sps_order(path: Path, output_date_format: str) -> SPSOrder:
    rows = read_csv_rows(path)
    header_index, header = find_header_row(rows, REQUIRED_SPS_HEADERS, "SPS PO")
    records = [row_as_dict(header, row) for row in rows[header_index + 1 :] if any(row)]

    header_records = [
        record for record in records if record.get("Record Type", "").upper() == "H"
    ]
    if len(header_records) != 1:
        raise ConversionError(
            f"Expected exactly one SPS H/header record; found {len(header_records)}"
        )
    order_header = header_records[0]

    po_number = require(order_header.get("PO Number", ""), "PO Number")
    po_date = parse_date(order_header.get("PO Date", ""), "PO Date", output_date_format)
    ship_date = parse_date(
        order_header.get("Ship Dates", ""), "Ship Date", output_date_format
    )
    requested_date = parse_date(
        order_header.get("Requested Delivery Date", ""),
        "Requested Delivery Date",
        output_date_format,
    )
    po_total = parse_decimal(order_header.get("PO Total Amount", ""), "PO Total Amount")

    detail_records = [
        record for record in records if record.get("Record Type", "").upper() == "D"
    ]
    if not detail_records:
        raise ConversionError("No SPS item detail records were found")

    items: list[SPSItem] = []
    seen_lines: set[str] = set()
    for position, record in enumerate(detail_records, start=1):
        line_number = record.get("PO Line #", "").strip() or f"detail #{position}"
        if line_number in seen_lines:
            raise ConversionError(f"Duplicate SPS item line detected: {line_number}")
        seen_lines.add(line_number)

        product_number = require(
            record.get("Buyers Catalog or Stock Keeping #", ""),
            f"ProductNumber on {line_number}",
        )
        quantity = parse_decimal(
            record.get("Qty Ordered", ""), f"Qty Ordered on {line_number}"
        )
        unit_price = parse_decimal(
            record.get("Unit Price", ""), f"Unit Price on {line_number}"
        )
        items.append(
            SPSItem(
                line_number=line_number,
                product_number=product_number,
                description=record.get("Product/Item Description", "").strip(),
                quantity=quantity,
                unit_price=unit_price,
                uom=map_uom(record.get("Unit of Measure", ""), line_number),
            )
        )

    warnings: list[str] = []
    address_2 = order_header.get("Ship To Address 2", "").strip()
    if address_2:
        warnings.append(
            "SPS Ship To Address 2 is populated; it will be used only if the template "
            "contains ShipToAddress2."
        )

    ship_to = {
        "ShipToName": require(order_header.get("Ship To Name", ""), "Ship To Name"),
        "ShipToAddress": require(
            order_header.get("Ship To Address 1", ""), "Ship To Address 1"
        ),
        "ShipToAddress2": address_2,
        "ShipToCity": require(order_header.get("Ship To City", ""), "Ship To City"),
        "ShipToState": require(order_header.get("Ship To State", ""), "Ship To State"),
        "ShipToZip": require(order_header.get("Ship to Zip", ""), "Ship To Zip"),
        "ShipToCountry": COUNTRY_MAP.get(
            require(order_header.get("Ship To Country", ""), "Ship To Country").upper(),
            order_header.get("Ship To Country", "").strip(),
        ),
    }

    calculated_total = sum((item.extended_amount for item in items), Decimal("0"))
    if abs(calculated_total - po_total) > Decimal("0.01"):
        raise ConversionError(
            f"Amount mismatch: items total {calculated_total:.2f}, "
            f"SPS PO total {po_total:.2f}"
        )

    return SPSOrder(
        po_number=po_number,
        po_date=po_date,
        ship_date=ship_date,
        requested_delivery_date=requested_date,
        po_total=po_total,
        ship_to=ship_to,
        items=items,
        warnings=warnings,
    )


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def select_fields(header: Sequence[str], values: dict[str, str]) -> list[str]:
    return [values.get(column, "") for column in header]


def build_output_rows(
    order: SPSOrder,
    so_header: list[str],
    item_header: list[str],
    reference_so: dict[str, str],
    reference_item: dict[str, str],
    extra_so_defaults: dict[str, str] | None = None,
    part_descriptions: dict[str, str] | None = None,
) -> list[list[str]]:
    so_values = dict(reference_so)
    so_values.update(SO_DEFAULTS)
    if extra_so_defaults:
        so_values.update(extra_so_defaults)
    so_values.update(order.ship_to)
    so_values.update(
        {
            "Flag": "SO",
            "SONum": "",
            "PONum": order.po_number,
            "Date": order.po_date,
            "OrderDateScheduled": order.ship_date,
            "CF-Earliest Ship": order.ship_date,
            "CF-Latest Ship": order.ship_date,
            "CF-Requested Delivery": order.requested_delivery_date,
        }
    )

    # Fishbowl's importer uses the selected import template for field definitions.
    # The import CSV itself must start with an SO data row, not column-name rows.
    rows = [select_fields(so_header, so_values)]
    for item in order.items:
        description = item.description
        if part_descriptions is not None:
            if item.product_number not in part_descriptions:
                raise ConversionError(
                    f"ProductNumber {item.product_number} was not found in Part.csv"
                )
            description = part_descriptions[item.product_number]
        item_values = dict(reference_item)
        item_values.update(ITEM_DEFAULTS)
        item_values.update(
            {
                "Flag": "Item",
                "SONum": "",
                "ProductNumber": item.product_number,
                "ProductDescription": description,
                "ProductQuantity": decimal_text(item.quantity),
                "UOM": item.uom,
                "ProductPrice": decimal_text(item.unit_price),
                "ItemDateScheduled": order.ship_date,
            }
        )
        rows.append(select_fields(item_header, item_values))
    return rows


def validate_output_rows(
    rows: Sequence[Sequence[str]],
    so_header: Sequence[str],
    item_header: Sequence[str],
    expected_items: int,
) -> None:
    if not rows or not rows[0] or rows[0][0] != "SO":
        raise ConversionError("Output line 1 must be an 'SO' data row")
    if len(rows[0]) != len(so_header):
        raise ConversionError("Generated SO row column count differs from SO header")
    item_rows = rows[1:]
    if len(item_rows) != expected_items:
        raise ConversionError(
            f"Output item count {len(item_rows)} differs from SPS count {expected_items}"
        )
    if any(len(row) != len(item_header) for row in item_rows):
        raise ConversionError("Generated Item row column count differs from Item header")
    if any(not row or row[0] != "Item" for row in item_rows):
        raise ConversionError("Every Item row must use the 'Item' flag")
    if rows[0][so_header.index("SONum")] != "":
        raise ConversionError("SONum must be blank on the SO row")
    if "SONum" in item_header and any(
        row[item_header.index("SONum")] for row in item_rows
    ):
        raise ConversionError("SONum must be blank on every Item row")


def write_csv(path: Path, rows: Iterable[Sequence[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Fishbowl treats a UTF-8 BOM as part of the first Flag value. The reference
    # export is plain ASCII/UTF-8, so deliberately write UTF-8 without a BOM.
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerows(rows)


def print_summary(order: SPSOrder, output: Path) -> None:
    quantity_total = sum((item.quantity for item in order.items), Decimal("0"))
    calculated_total = sum((item.extended_amount for item in order.items), Decimal("0"))
    print("Conversion successful")
    print(f"PO Number: {order.po_number}")
    print(f"PO Date -> Date: {order.po_date}")
    print(f"Ship Dates -> scheduled/earliest/latest: {order.ship_date}")
    print(f"Requested Delivery Date: {order.requested_delivery_date}")
    print(
        "Ship To: "
        f"{order.ship_to['ShipToName']}, {order.ship_to['ShipToAddress']}, "
        f"{order.ship_to['ShipToCity']}, {order.ship_to['ShipToState']} "
        f"{order.ship_to['ShipToZip']}, {order.ship_to['ShipToCountry']}"
    )
    print(f"Item rows: {len(order.items)}")
    print(f"Total quantity: {decimal_text(quantity_total)}")
    print(f"Calculated item amount: ${calculated_total:.2f}")
    print(f"SPS PO total amount: ${order.po_total:.2f}")
    print(f"Amount difference: ${abs(calculated_total - order.po_total):.2f}")
    print("Template column order: PASS")
    print("SO/Item row widths: PASS")
    print("Required item fields: PASS")
    print("Duplicate/missing item check: PASS")
    print("Output encoding: UTF-8 without BOM")
    print(f"Output: {output}")
    for warning in order.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--po", required=True, type=Path, help="SPS Commerce PO CSV")
    parser.add_argument("--template", required=True, type=Path, help="Fishbowl template CSV")
    parser.add_argument(
        "--parts",
        required=True,
        type=Path,
        help="Fishbowl Part.csv used for ProductDescription mapping",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        help="Optional normal Fishbowl SO CSV used for additional defaults",
    )
    parser.add_argument("--output", required=True, type=Path, help="Output Fishbowl SO CSV")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        so_header, item_header = load_template(args.template)
        if args.reference:
            reference_so, reference_item, reference_rows = load_reference_defaults(
                args.reference, so_header, item_header
            )
        else:
            reference_so, reference_item, reference_rows = {}, {}, []
        output_date_format = infer_date_format(reference_rows, so_header)
        order = parse_sps_order(args.po, output_date_format)
        part_descriptions = load_part_descriptions(args.parts)
        output_rows = build_output_rows(
            order,
            so_header,
            item_header,
            reference_so,
            reference_item,
            part_descriptions=part_descriptions,
        )
        validate_output_rows(output_rows, so_header, item_header, len(order.items))
        write_csv(args.output, output_rows)

        # Re-read the completed artifact so disk encoding/CSV serialization are validated too.
        written_rows = read_csv_rows(args.output)
        validate_output_rows(written_rows, so_header, item_header, len(order.items))
        print_summary(order, args.output)
        print(
            f"Part description mappings: "
            f"{len(order.items)}/{len(order.items)} matched"
        )
        return 0
    except (ConversionError, OSError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
