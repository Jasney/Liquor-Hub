import csv
import hashlib
import io
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .catalogue import FIELDS, SOURCE, key, normalized, parse_catalogue
from .models import Category, Product, ProductVariant


class CatalogueExtractionTests(TestCase):
    def test_complete_extract_and_provenance(self):
        source, index, categories, rows, errors, duplicates, _ = parse_catalogue(SOURCE)
        self.assertEqual(len(index), 44)  # Source contradicts the requested 42.
        self.assertEqual(set(categories), set(index))
        self.assertEqual(len(rows), 1337)
        self.assertEqual(errors, [])
        self.assertEqual(len(duplicates), 1)
        lines = source.splitlines()
        for row in rows:
            fragment = lines[row["source_line_start"] - 1:row["source_line_end"]]
            self.assertEqual(" ".join(fragment[:-2]), row["source_name"])
            self.assertEqual(fragment[-2], row["unit"])
            self.assertEqual(fragment[-1], row["raw_price"])

    def test_units_packs_wrapped_names_and_missing_sizes_are_preserved(self):
        rows = parse_catalogue(SOURCE)[3]
        by_name = {row["source_name"]: row for row in rows}
        conflicting = by_name["Gordon's Pink Can 330ml"]
        self.assertEqual(conflicting["size"], "330ml")
        self.assertEqual(conflicting["unit"], "1 Litre")
        self.assertIn("size_column_litre_conflict", conflicting["flags"])
        pack = by_name["Tusker Lager 500ml 6 Pack"]
        single = by_name["Tusker Lager 500ml"]
        self.assertIn("6 Pack", pack["product_name"])
        self.assertNotEqual(pack["product_key"], single["product_key"])
        self.assertEqual(by_name["Casillero Del Diablo Cabernet Sauvignon 750ml"]["size"], "750ml")
        self.assertEqual(by_name["White Cap Crisp"]["size"], "")
        self.assertEqual(by_name["Fireball Cinnamon IL"]["size"], "")

    def test_resolved_subset_imports_all_categories_and_repeats_safely(self):
        # TestCase uses Django's disposable test database, never db.sqlite3.
        path = SOURCE.with_name("liquor_catalogue_2026.csv")
        categories = SOURCE.with_name("categories_2026.csv")
        options = {
            "csv": path, "skip_pending": True,
            "approved_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "approved_categories_sha256": hashlib.sha256(categories.read_bytes()).hexdigest(),
            "stdout": io.StringIO(),
        }
        call_command("import_liquor_catalog", **options)
        first_counts = (Category.objects.count(), Product.objects.count(), ProductVariant.objects.count())
        call_command("import_liquor_catalog", **options)
        self.assertEqual(first_counts, (Category.objects.count(), Product.objects.count(), ProductVariant.objects.count()))
        self.assertEqual(Category.objects.count(), 44)
        ready_count = sum(row["review_status"] == "ready" for row in parse_catalogue(SOURCE)[3])
        self.assertEqual(ProductVariant.objects.count(), ready_count)
        self.assertFalse(ProductVariant.objects.exclude(stock_quantity=0).exists())
        session = self.client.session
        session["age_verified_at"] = "test-confirmed"
        session.save()
        for category in Category.objects.all():
            response = self.client.get(reverse("category", args=[category.slug]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["category"].pk, category.pk)


class CatalogueImportTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.csv_path = Path(self.directory.name) / "catalog.csv"
        self.categories_path = self.csv_path.with_name("categories_2026.csv")
        with self.categories_path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["category", "display_order", "entry_count"])
            writer.writerow(["Gin", 1, 2])
        self.rows = [self.row("1", "350ml", "1400"), self.row("2", "750ml", "2600")]
        self.write_rows()

    def row(self, row_id, size, price):
        product_key = key("LHC-P-", "gin", "client gin")
        return dict.fromkeys(FIELDS, "") | {
            "row_id": row_id, "category": "Gin", "product_name": "Client Gin",
            "size": size, "variant_label": size, "unit": "Piece", "price_ksh": price,
            "product_key": product_key, "variant_key": key("LHC-V-", product_key, size, "piece"),
            "review_status": "ready",
        }

    def write_rows(self):
        with self.csv_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(self.rows)

    def run_import(self, approved=True, **options):
        if approved:
            options.update(
                approved_sha256=hashlib.sha256(self.csv_path.read_bytes()).hexdigest(),
                approved_categories_sha256=hashlib.sha256(self.categories_path.read_bytes()).hexdigest(),
            )
        output = io.StringIO()
        call_command("import_liquor_catalog", csv=self.csv_path, stdout=output, **options)
        return output.getvalue()

    def test_dry_run_and_default_have_no_database_writes(self):
        with CaptureQueriesContext(connection) as captured:
            self.run_import(approved=False, dry_run=True)
            self.run_import(approved=False)
        self.assertFalse(Product.objects.exists())
        self.assertFalse(Category.objects.exists())
        for query in captured:
            self.assertNotRegex(query["sql"].upper(), r"^\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|BEGIN|SAVEPOINT)")

    def test_approval_digest_mismatch_prevents_writes(self):
        with self.assertRaisesMessage(CommandError, "digests do not match"):
            self.run_import(approved=False, approved_sha256="not-approved")
        self.assertFalse(Category.objects.exists())

    def test_pending_rows_block_full_import(self):
        self.rows[0]["flags"] = "missing_size"
        self.rows[0]["review_status"] = "pending"
        self.write_rows()
        with self.assertRaisesMessage(CommandError, "Pending entries"):
            self.run_import()
        self.assertFalse(Category.objects.exists())
        self.run_import(skip_pending=True)
        self.assertEqual(ProductVariant.objects.count(), 1)

    def test_ready_status_cannot_bypass_flags(self):
        self.rows[0]["flags"] = "missing_size"
        self.write_rows()
        with self.assertRaisesMessage(CommandError, "Pending entries"):
            self.run_import()

    def test_repeat_import_preserves_manual_edits_and_stock(self):
        self.run_import()
        product = Product.objects.get()
        product.name = "Client renamed gin"
        product.description = "Client-provided description"
        product.image = "products/client-photo.jpg"
        product.is_active = False
        product.save()
        variant = product.variants.get(bottle_size="750ml")
        variant.stock_quantity = 17
        variant.price = Decimal("2800")
        variant.sale_price = Decimal("2700")
        variant.unit = "Client unit"
        variant.is_available = False
        variant.save()
        self.run_import()
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductVariant.objects.count(), 2)
        product.refresh_from_db()
        variant.refresh_from_db()
        self.assertEqual(product.name, "Client renamed gin")
        self.assertEqual(product.description, "Client-provided description")
        self.assertEqual(product.image.name, "products/client-photo.jpg")
        self.assertFalse(product.is_active)
        self.assertEqual(variant.stock_quantity, 17)
        self.assertEqual(variant.price, Decimal("2800"))
        self.assertEqual(variant.sale_price, Decimal("2700"))
        self.assertEqual(variant.unit, "Client unit")
        self.assertFalse(variant.is_available)
        self.run_import(update_prices=True)
        variant.refresh_from_db()
        self.assertEqual(variant.price, Decimal("2600"))
        self.assertEqual(variant.stock_quantity, 17)

    def test_new_variants_have_zero_stock_and_separate_unit(self):
        self.run_import()
        for variant in ProductVariant.objects.all():
            self.assertEqual(variant.unit, "Piece")
            self.assertEqual(variant.stock_quantity, 0)
            self.assertFalse(variant.in_stock)

    def test_duplicate_and_bad_price_rejected_before_writes(self):
        self.rows[1] = self.rows[0] | {"row_id": "2", "price_ksh": "-1"}
        self.write_rows()
        with self.assertRaisesMessage(CommandError, "duplicate selected variant"):
            self.run_import()
        self.assertFalse(Category.objects.exists())

    def test_transaction_rolls_back_partial_import(self):
        original = ProductVariant.objects.get_or_create
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Simulated failure")
            return original(*args, **kwargs)

        with patch.object(ProductVariant.objects, "get_or_create", side_effect=fail_second):
            with self.assertRaisesMessage(RuntimeError, "Simulated failure"):
                self.run_import()
        self.assertFalse(Category.objects.exists())
        self.assertFalse(Product.objects.exists())
        self.assertFalse(ProductVariant.objects.exists())

    def test_existing_unlinked_products_are_not_overwritten_or_duplicated(self):
        category = Category.objects.create(name="Gin")
        Product.objects.create(name="Client Gin", category=category, description="Existing data")
        with self.assertRaisesMessage(CommandError, "explicit reconciliation required"):
            self.run_import()
        self.assertEqual(Product.objects.get().description, "Existing data")

    def test_imported_variant_is_preserved_in_cart(self):
        self.run_import()
        variant = ProductVariant.objects.get(bottle_size="350ml")
        variant.stock_quantity = 2
        variant.save()
        session = self.client.session
        session["age_verified_at"] = "test-confirmed"
        session.save()
        self.client.post(reverse("cart_add", args=[variant.pk]), {"quantity": 1})
        self.assertEqual(self.client.session["cart"], {str(variant.pk): 1})
        self.assertContains(self.client.get(reverse("cart")), "1400")
