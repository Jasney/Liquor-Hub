"""Validate/plan read-only by default; write only an explicitly approved CSV."""

import csv
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from store.catalogue import normalized, normalized_size
from store.models import Category, Product, ProductVariant


DEFAULT_CSV = Path(__file__).resolve().parents[3] / "catalogue_data/catalogue_cleaned.csv"


def read_catalogue(path, categories_path):
    errors = []
    try:
        data = path.read_bytes()
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"row_id", "category", "product_name", "variant_label", "size", "unit", "price_ksh", "product_key", "variant_key", "flags", "review_status", "duplicate_of"}
            if not required.issubset(reader.fieldnames or []):
                raise CommandError("CSV is missing required catalogue columns.")
            rows = list(reader)
        with categories_path.open(encoding="utf-8-sig", newline="") as stream:
            categories = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as error:
        raise CommandError(str(error)) from error
    if not rows or not categories:
        raise CommandError("Catalogue and category manifest must not be empty.")
    category_names = set()
    for category in categories:
        name = category.get("category", "")
        if not name or len(name) > 100 or normalized(name) in category_names:
            errors.append(f"Invalid or duplicate manifest category: {name!r}")
        category_names.add(normalized(name))
        try:
            if int(category["display_order"]) < 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            errors.append(f"Invalid category order: {name!r}")
    row_ids, variant_keys, product_definitions = set(), set(), {}
    pending, excluded, selected = [], [], []
    natural_variants = set()
    for row in rows:
        row_id = row.get("row_id", "")
        if None in row or any(value is None for value in row.values()):
            errors.append(f"{row_id}: malformed CSV row (incorrect column count)")
            continue
        if not row_id or row_id in row_ids:
            errors.append(f"Duplicate or empty row ID: {row_id!r}")
        row_ids.add(row_id)
        if row.get("review_status") == "exclude":
            excluded.append(row_id)
            continue
        status = row.get("review_status")
        if status not in {"ready", "pending", "approved"}:
            errors.append(f"{row_id}: invalid review_status")
        if status == "pending" or (row.get("flags") and status != "approved"):
            pending.append(row_id)
            continue
        if (
            row.get("record_type", "physical_product") != "physical_product"
            or row.get("storefront_eligible", "true").lower() != "true"
            or re.search(r"^(?:Delivery\b|courier$|Miscellaneous Sales$)", row.get("source_name") or row.get("product_name", ""), re.I)
        ):
            errors.append(f"{row_id}: service or excluded entry cannot be imported as a physical product")
        if row.get("duplicate_of"):
            errors.append(f"{row_id}: duplicate row must be explicitly excluded or resolved")
        for field, limit in (("category", 100), ("product_name", 180), ("variant_label", 40), ("unit", 40)):
            value = row.get(field)
            if not value or len(value) > limit or value != value.strip():
                errors.append(f"{row_id}: invalid {field} (required, maximum {limit} characters)")
        if row.get("category", "") not in {entry["category"] for entry in categories}:
            errors.append(f"{row_id}: category absent from manifest")
        try:
            price = Decimal(row.get("price_ksh", ""))
            if not price.is_finite() or not Decimal("0") < price <= Decimal("99999999.99") or price.as_tuple().exponent < -2:
                raise InvalidOperation
            row["price"] = price
        except (InvalidOperation, ValueError):
            errors.append(f"{row_id}: invalid KSh price")
        for field, prefix in (("product_key", "LHC-P-"), ("variant_key", "LHC-V-")):
            if not re.fullmatch(prefix + r"[0-9a-f]{32}", row.get(field, "")):
                errors.append(f"{row_id}: invalid {field}")
        product_definition = (normalized(row.get("category", "")), normalized(row.get("product_name", "")))
        product_key = row.get("product_key")
        if product_key in product_definitions and product_definitions[product_key] != product_definition:
            errors.append(f"{row_id}: product key has inconsistent category/name")
        product_definitions[product_key] = product_definition
        variant_key = row.get("variant_key")
        identity = (normalized(row.get("category", "")), normalized(row.get("product_name", "")), normalized_size(row.get("size", "")), normalized(row.get("unit", "")) if not row.get("size") else "")
        if variant_key in variant_keys or identity in natural_variants:
            errors.append(f"{row_id}: duplicate selected variant")
        variant_keys.add(variant_key)
        natural_variants.add(identity)
        selected.append(row)
    if errors:
        raise CommandError("Catalogue validation failed:\n" + "\n".join(errors))
    # The manifest is included in the approval digest so category changes cannot
    # silently escape approval by leaving the product CSV unchanged.
    return rows, selected, categories, pending, excluded, hashlib.sha256(data).hexdigest(), hashlib.sha256(categories_path.read_bytes()).hexdigest()


def table_columns(model):
    with connection.cursor() as cursor:
        return {c.name for c in connection.introspection.get_table_description(cursor, model._meta.db_table)}


def build_plan(selected, categories, update_prices=False):
    """SELECT only. Also works before the prepared migration is applied."""
    category_columns = table_columns(Category)
    product_columns = table_columns(Product)
    variant_columns = table_columns(ProductVariant)
    schema_ready = (
        "display_order" in category_columns
        and "catalogue_key" in product_columns
        and {"catalogue_key", "unit"}.issubset(variant_columns)
    )
    existing_categories = list(Category.objects.order_by().values("pk", "name"))
    product_fields = ["pk", "name", "category_id"] + (["catalogue_key"] if "catalogue_key" in product_columns else [])
    variant_fields = ["pk", "product_id", "price", "bottle_size"] + (["catalogue_key"] if "catalogue_key" in variant_columns else [])
    existing_products = list(Product.objects.order_by().values(*product_fields))
    existing_variants = list(ProductVariant.objects.order_by().values(*variant_fields))
    by_category = {normalized(c["name"]): c for c in existing_categories}
    by_product = {p["catalogue_key"]: p for p in existing_products if p.get("catalogue_key")}
    by_variant = {v["catalogue_key"]: v for v in existing_variants if v.get("catalogue_key")}
    natural_products = {(p["category_id"], normalized(p["name"])): p for p in existing_products}
    statistics = Counter(categories_create=0, categories_reuse=0, products_create=0, products_reuse=0,
                         variants_create=0, variants_reuse=0, prices_update=0, prices_differ_preserved=0)
    errors, seen_products = [], set()
    for category in categories:
        existing = by_category.get(normalized(category["category"]))
        if existing and existing["name"] != category["category"]:
            errors.append(f"Category spelling/case conflict: {category['category']} vs {existing['name']}")
        statistics["categories_reuse" if existing else "categories_create"] += 1
    for row in selected:
        product = by_product.get(row["product_key"])
        variant = by_variant.get(row["variant_key"])
        category = by_category.get(normalized(row["category"]))
        if row["product_key"] not in seen_products:
            seen_products.add(row["product_key"])
            statistics["products_reuse" if product else "products_create"] += 1
            if not product and category and (category["pk"], normalized(row["product_name"])) in natural_products:
                errors.append(f"{row['row_id']}: unlinked existing product with this category/name; explicit reconciliation required")
        if variant and (not product or variant["product_id"] != product["pk"]):
            errors.append(f"{row['row_id']}: existing variant belongs to another product")
        if product and not variant:
            if any(v["product_id"] == product["pk"] and normalized(v["bottle_size"]) == normalized(row["variant_label"]) for v in existing_variants):
                errors.append(f"{row['row_id']}: unlinked or colliding existing variant label")
        statistics["variants_reuse" if variant else "variants_create"] += 1
        if variant and variant["price"] != row["price"]:
            statistics["prices_update" if update_prices else "prices_differ_preserved"] += 1
    if errors:
        raise CommandError("Import conflicts:\n" + "\n".join(errors))
    return dict(statistics), schema_ready


class Command(BaseCommand):
    help = "Validate and plan the official catalogue; writes require an exact-file approval digest."

    def add_arguments(self, parser):
        parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
        parser.add_argument("--categories", type=Path, help="Defaults to categories_2026.csv beside the product CSV")
        parser.add_argument("--dry-run", action="store_true", help="Strictly read-only; no temporary DB writes")
        parser.add_argument("--approved-sha256", help="CSV SHA-256 approved by the user; enables writing")
        parser.add_argument("--approved-categories-sha256", help="Approved category manifest SHA-256; required for writes")
        parser.add_argument("--skip-pending", action="store_true", help="Explicitly plan/import only resolved rows")
        parser.add_argument("--update-prices", action="store_true", help="Explicitly replace existing regular prices only")

    def handle(self, *args, **options):
        path = options["csv"]
        categories_path = options["categories"] or path.with_name("categories_2026.csv")
        rows, selected, categories, pending, excluded, digest, category_digest = read_catalogue(path, categories_path)
        dry_run = options["dry_run"] or not options["approved_sha256"]
        stats, schema_ready = build_plan(selected, categories, options["update_prices"])
        summary = {
            "mode": "READ ONLY" if dry_run else "APPROVED IMPORT",
            "csv_sha256": digest, "categories_sha256": category_digest,
            "source_entries": len(rows), "selected_entries": len(selected),
            "pending_entries": len(pending), "excluded_entries": len(excluded),
            "schema_ready": schema_ready, "statistics": stats,
            "blocked_by_pending": bool(pending and not options["skip_pending"]),
        }
        self.stdout.write(json.dumps(summary, indent=2))
        if dry_run:
            self.stdout.write("No database writes performed. Actual import requires user approval and both file digests.")
            if pending and not options["skip_pending"]:
                self.stdout.write("BLOCKED: resolve pending entries or explicitly choose --skip-pending for a partial import.")
            return
        if options["approved_sha256"] != digest or options["approved_categories_sha256"] != category_digest:
            raise CommandError("Approval digests do not match both current files; no import performed.")
        if pending and not options["skip_pending"]:
            raise CommandError("Pending entries block this import. Review them before importing the full catalogue.")
        if not schema_ready:
            raise CommandError("Apply the prepared store migration after approval before importing.")
        # Recheck under a transaction; lock existing catalogue objects. Unique
        # catalogue keys also protect against duplicate concurrent creation.
        with transaction.atomic():
            list(Category.objects.select_for_update().values_list("pk", flat=True))
            list(Product.objects.select_for_update().values_list("pk", flat=True))
            list(ProductVariant.objects.select_for_update().values_list("pk", flat=True))
            stats, _ = build_plan(selected, categories, options["update_prices"])
            category_objects = {}
            for entry in categories:
                category, _ = Category.objects.get_or_create(
                    name=entry["category"], defaults={"display_order": int(entry["display_order"]), "is_active": True},
                )
                category_objects[entry["category"]] = category
            products = {}
            for row in selected:
                if row["product_key"] not in products:
                    product, _ = Product.objects.get_or_create(
                        catalogue_key=row["product_key"],
                        defaults={"name": row["product_name"], "category": category_objects[row["category"]]},
                    )
                    products[row["product_key"]] = product
                variant, created = ProductVariant.objects.get_or_create(
                    catalogue_key=row["variant_key"],
                    defaults={"product": products[row["product_key"]], "bottle_size": row["variant_label"],
                              "unit": row["unit"], "price": row["price"], "stock_quantity": 0},
                )
                if not created and options["update_prices"] and variant.price != row["price"]:
                    variant.price = row["price"]
                    variant.save(update_fields=["price"])
        self.stdout.write(self.style.SUCCESS("Import committed: " + json.dumps(stats, sort_keys=True)))
