# Liquor Hub Diani

A Django demonstration storefront for a local liquor shop in Diani. It includes a responsive customer site, age-confirmation gate, catalog administration, session cart, stock-aware checkout, order management, and clearly labeled mock payment flows.

> This project is a demonstration. Seeded prices and inventory are sample values, no live orders are fulfilled, and mock payments never move money.

## Requirements

- Python 3.12 or another Django 5.2-supported Python version
- pip
- Optional: PostgreSQL for production-like local testing

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Visit `http://127.0.0.1:8000/`. Confirm that you are 18 or older at the age gate. The local administrator is at `http://127.0.0.1:8000/admin/`.

If your default Python is not compatible with Django 5.2, create the virtual environment with a supported Python interpreter (for example, `python3.12 -m venv .venv`).

## Product administration

Sign in to `/admin/` with a superuser or a staff account with product permissions. **All products** supports every catalogue category, category filtering, search, product details, images, and variant inlines. **Categories** supports the original category names, URL slugs, images, activity and display ordering. The existing **Gin**, **Whisky**, **Wine**, and **Beer** shortcuts remain available for the legacy categories. Each variant has separate size and packaging-unit fields, prices, stock and availability. Orders and payments remain available from the admin navigation.

Run `python manage.py remove_demo` to remove the original sample listings before entering the client's catalog. Samples referenced by orders are hidden instead, preserving order history. Existing categories are retained. Do not run `seed_demo` on the client catalog: it restores sample products, prices, and stock.

For client access, create a staff user under **Users**, grant the Product view/add/change permissions and Product image view/add/change/delete permissions, and share their individual login securely. They can manage the catalog at `/admin/` without superuser access.

## Official 2026 catalogue preparation

The supplied extract and generated review artifacts are in `catalogue_data/`. The source contains **44** categories, despite the task notes saying 42. Keep all original categories until the client resolves this discrepancy. The original PDF has not been supplied for visual verification.

Rebuild the proposed CSV and report without database access:

```bash
python -m store.catalogue
python -m store.catalogue_cleaning
python manage.py import_liquor_catalog --csv catalogue_data/catalogue_cleaned.csv --dry-run
```

Read `catalogue_data/VALIDATION_REPORT.md`, `validation_report.json`, and `IMPORT_DRY_RUN.txt`. Each CSV row includes its source name, table unit, price, PDF page, text lines and review flags. The parser leaves units separate from capacity and preserves pack counts, flavours, editions, kits and pods. Near matches are flagged, never merged. Regenerating the artifacts resets manual review decisions, so keep a separately named reviewed CSV and use `--csv` to import it.

The latest cleaning outputs are `catalogue_data/catalogue_cleaned.csv` and `catalogue_data/catalogue_needs_review.csv`; see `CLEANING_REPORT.md` and `CLEANED_IMPORT_DRY_RUN.txt` for the current proposal. The importer defaults to the cleaned CSV. It contains 1,047 unique product groups and 1,193 variants; 143 entries remain in review and one exact duplicate copy is removed with a full audit in `CLEANING_REPORT.json`. Conflicting table units keep the explicit-name size as a pending candidate, not an approved correction. Original categories and prices are retained. Different flavours, colours, editions and packaging stay separate; possible name aliases remain pending. Delivery/courier charges are retained as service configuration records in the review file, and Miscellaneous Sales is excluded pending review. The importer refuses service/non-storefront records as physical products, even if marked approved. No catalogue imports have been performed.

Migration `0005` adds category ordering, separate variant units and unique nullable catalogue identifiers. It was applied locally to repair the website/admin schema mismatch; see `catalogue_data/LOCAL_SERVER_STATUS.md`. The earlier validation report describes the state before that repair. No catalogue products or variants were imported. Existing data is preserved by the additive migration.

After approval, resolve flagged rows with `review_status=approved` or `exclude`, correct any uncertain fields, and run another dry-run. Keep the immutable source keys to preserve identity across later edits. If merging alias products, assign the surviving product key/name consistently and exclude duplicate variants explicitly. The report's duplicate references remain an audit trail.

Only after the user's approval, apply migrations and pass both SHA-256 values printed by the dry-run:

```bash
python manage.py migrate
python manage.py import_liquor_catalog --csv catalogue_data/catalogue_cleaned.csv --approved-sha256 APPROVED_CSV_DIGEST --approved-categories-sha256 APPROVED_CATEGORY_MANIFEST_DIGEST
```

Pending rows block the entire import by default. `--skip-pending` is an explicit option for an approved partial import; it is not a complete-catalogue import. The manifest preserves all original categories even when no rows are selected in a category. The existing legacy Whisky/Wine/Beer categories remain intact rather than being silently renamed or merged.

The importer uses one transaction, stable catalogue keys, and conflict checks. Repeats preserve existing descriptions, names, category assignments, images, slugs, stock, availability, sale prices, units and regular prices. `--update-prices` explicitly replaces only existing regular prices. New variants have zero stock because the price list contains no inventory counts. Unlinked pre-existing products with matching names/categories require explicit reconciliation. No absent products are deleted, no samples are seeded, and no images or descriptions are invented. Category names/order are set for new categories; existing category settings are preserved.

Give clients the required Category permissions as well if they should manage categories. No client accounts or permissions were changed during catalogue preparation. Automated import tests use Django's disposable test database.

## Product image finder

The optional image finder uses permitted supplier JSON feeds through a source-independent adapter interface to collect up to five candidate photographs per database product. Discovery never downloads original images or publishes them. Django Admin separates visual match review, commercial rights verification and explicit publication; existing manual images are preserved. See [setup, commands, review permissions and reports](docs/PRODUCT_IMAGE_FINDER.md).

```bash
python manage.py find_product_images --dry-run --limit 20 --missing-only
python manage.py find_product_images --feed /path/to/permitted-supplier-feed.json --limit 20 --category gin --report image-discovery.json
```

Discovery requires an explicit supplier feed with documented permission. A live supplier website adapter is a separate next step. The imported catalogue remains editable through Django Admin.

## Payments

`PAYMENT_MODE=mock` is the safe local default. The local mock result screen only runs with `DEBUG=True`; a simulated result is explicitly labeled and no customer payment data is collected.

For unified hosted M-Pesa, Visa and Mastercard checkout through PesaPal sandbox:

1. Register the public HTTPS `/payments/pesapal/callback/` URL as a PesaPal IPN and set the returned notification ID as `PESAPAL_IPN_ID`.
2. Set `PAYMENT_MODE=pesapal` and configure `PESAPAL_CONSUMER_KEY`, `PESAPAL_CONSUMER_SECRET`, `PESAPAL_IPN_ID`, and the sandbox `PESAPAL_BASE_URL`.
3. The browser return and IPN are treated as notices only. The server queries PesaPal status and compares merchant reference, amount, currency and confirmation reference before finalizing an order.
4. Configure `ORDER_NOTIFICATION_EMAIL` and a production email backend. Run `python manage.py deliver_order_notifications` on a schedule to deliver/retry verified-purchase email notifications.
5. Run `python manage.py expire_stock_reservations` on a schedule. Expired pending payments are held as uncertain for reconciliation instead of releasing inventory.

Receipt PDFs are generated from the persisted receipt snapshot and require the declared ReportLab dependency. Receipt downloads require the same browser session and order token created at checkout. This project has no customer account system, so it does not provide cross-device receipt history.

The legacy direct Daraja sandbox STK Push mode remains available:

1. Set `PAYMENT_MODE=daraja`.
2. Configure `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET`, `DARAJA_SHORTCODE`, `DARAJA_PASSKEY`, and `DARAJA_CALLBACK_URL` from Safaricom's sandbox.
3. Set a long random `DARAJA_CALLBACK_TOKEN`, and configure the callback URL in the sandbox to point to the public HTTPS `/mpesa/callback/` endpoint with the token appended by the app.
4. Keep credentials private and test only with Safaricom's sandbox accounts.

STK Push acceptance does not mark an order paid. The callback checks its configured shared token and matches a saved checkout request, amount and receipt reference. Use PesaPal for provider-side status reconciliation and unified hosted card/M-Pesa methods; the direct Daraja callback does not perform the same server-side status query.

## Configuration and deployment preparation

Settings read environment variables and a local `.env` file. `.env` is ignored by Git; never commit credentials. SQLite is the default. Set `DATABASE_URL` to a PostgreSQL URL to use PostgreSQL. Set `DEBUG=False`, a strong `SECRET_KEY`, and production `ALLOWED_HOSTS` before deployment. Configure `CSRF_TRUSTED_ORIGINS` as needed. WhiteNoise serves collected static files. `REQUIRE_DELIVERY_AGE_VERIFICATION=True` prevents an order from being marked delivered in the admin until the delivery age-check box is confirmed:

```bash
python manage.py collectstatic --noinput
gunicorn config.wsgi:application
```

For Railway or Render, configure the environment values in the service dashboard, provision PostgreSQL, set the start command to `gunicorn config.wsgi:application`, and run `python manage.py migrate` and `python manage.py collectstatic --noinput` as release/build steps. Use persistent object storage for uploaded product images in production; local media storage is for development only. Do not point a live domain, change DNS, or add live payment credentials without explicit approval.

The age confirmation is an initial user-interface safeguard, not legal compliance or proof of age. Confirm local rules, delivery age checks, licensing, tax, privacy and fulfillment policies before launch.

## Tests

```bash
python manage.py test store
```

Tests cover age gating, category/search behavior, cart totals, stock revalidation, checkout, mock and callback payment transitions, phone normalization, and admin access.
