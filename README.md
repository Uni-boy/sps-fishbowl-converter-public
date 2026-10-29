# SPS Commerce to Fishbowl Sales Order

A small Streamlit application that converts an SPS Commerce purchase-order CSV
into a Fishbowl Sales Order import CSV.

## What the manager does

1. Open the Streamlit application.
2. Use **Update Part catalog** when an immediate Fishbowl refresh is needed.
3. Upload one SPS Commerce PO CSV.
4. Review the item count, quantity, dates, address, and amount.
5. Download the validated Fishbowl CSV.

The Fishbowl template and an encrypted Part description catalog are bundled
with the application. The decryption key is stored only in Streamlit Secrets.
The original `Part.csv` is never committed to GitHub.

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
PARTS_ENCRYPTION_KEY = "private Fernet key"

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
- `encrypt_parts.py` — refreshes the encrypted Part description catalog
- `sync_parts_from_fishbowl.py` — reads and validates parts from the Fishbowl
  read-only API, logs out, and refreshes the encrypted catalog only when data
  changed
- `SalesOrder_template.csv` — Fishbowl column definition
- `data/parts.csv.fernet` — encrypted PartNumber/description catalog
- `requirements.txt` — Streamlit dependency

Every SPS ProductNumber must exist exactly once in the encrypted catalog and
have a non-empty `PartDescription`. Conversion stops on missing or duplicate
mappings.

## Refresh the Part catalog

Replace the local ignored `Part.csv`, keep the existing encryption key in
`.streamlit/secrets.toml`, and run:

```bash
python3 encrypt_parts.py
```

Commit only the regenerated `data/parts.csv.fernet`. Do not commit `Part.csv`
or `.streamlit/secrets.toml`.

## Automatic Part catalog refresh

The GitHub Actions workflow in `.github/workflows/update-parts.yml` checks
Fishbowl every day at 8:00 AM America/New_York. It schedules both UTC offsets
used by Eastern Time and applies a timezone gate, so daylight-saving changes do
not shift the business time. A manual run is also available from the Actions
tab.

Create these repository Actions secrets before enabling the schedule:

- `FISHBOWL_USERNAME`
- `FISHBOWL_PASSWORD`
- `PARTS_ENCRYPTION_KEY`

The workflow uses the approved Fishbowl read-only app identity, reads the
`part` table in stable pages, validates every PartNumber and description, and
always attempts logout before ending. When the catalog changes, the workflow
commits only `data/parts.csv.fernet`; otherwise it creates no commit. The
credentials and decrypted CSV never enter the repository.

The Streamlit **Update Part catalog** button uses the same read-only API and
updates the running app immediately. Add `FISHBOWL_USERNAME` and
`FISHBOWL_PASSWORD` to the app's Streamlit Secrets. The button always attempts
Fishbowl logout, validates the full catalog, and keeps the runtime copy
encrypted. Runtime updates are temporary if the Streamlit container restarts;
the daily GitHub Actions job remains the permanent repository refresh.

The generated Fishbowl file contains no header rows and uses UTF-8 without a
BOM. It begins with one `SO` row followed by the order's `Item` rows sorted in
ascending natural order by `ProductNumber`/SKU. Sorting changes only row order;
the original SKU text, quantity, price, and other item values are preserved.
