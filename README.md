# SPS Commerce to Fishbowl Sales Order

A small Streamlit application that converts an SPS Commerce purchase-order CSV
into a Fishbowl Sales Order import CSV.

## What the manager does

1. Open the Streamlit application.
2. Upload one SPS Commerce PO CSV and the current Fishbowl `Part.csv`.
3. Review the item count, quantity, dates, address, and amount.
4. Download the validated Fishbowl CSV.

The Fishbowl template is bundled with the application. `Part.csv` remains
private and is uploaded at conversion time; it is never committed to GitHub.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Push these project files to GitHub.
2. Sign in at <https://share.streamlit.io/>.
3. Select **Create app** and choose the GitHub repository.
4. Set the entrypoint to `app.py`.
5. Deploy and share the resulting URL with the manager.

Configure private business defaults through Streamlit Secrets instead of
committing customer information to GitHub:

```toml
[fishbowl_defaults]
CustomerName = "your customer name"
CustomerContact = "your customer contact"
BillToName = "your billing name"
BillToAddress = "your billing street"
BillToCity = "your billing city"
BillToState = "your billing state"
BillToZip = "your billing ZIP"
BillToCountry = "your billing country"
CarrierName = "your carrier default"
TaxRateName = "your tax rate default"
PriorityId = "your priority ID"
Salesman = "your salesperson"
ShippingTerms = "your shipping terms"
PaymentTerms = "your payment terms"
FOB = "your FOB default"
QuickBooksClassName = "your QuickBooks class"
LocationGroupName = "your location group"
```

## Included files

- `app.py` — browser interface
- `convert_sps_po_to_fishbowl_so.py` — conversion and validation logic
- `SalesOrder_template.csv` — Fishbowl column definition
- `requirements.txt` — Streamlit dependency

Every SPS ProductNumber must exist exactly once in `Part.csv` and have a
non-empty `PartDescription`. Conversion stops on missing or duplicate mappings.

The generated Fishbowl file contains no header rows and uses UTF-8 without a
BOM. It begins with one `SO` row followed by the order's `Item` rows.
