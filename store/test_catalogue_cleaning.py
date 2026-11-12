import csv
import hashlib
import io
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .catalogue_cleaning import DEFAULT_INPUT, OUTPUT_FIELDS, clean_rows, prepare_cleaned_catalogue
from .management.commands.import_liquor_catalog import read_catalogue
from .models import Category, Product, ProductVariant


class CatalogueCleaningTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with DEFAULT_INPUT.open(newline="", encoding="utf-8") as stream:
            cls.source = list(csv.DictReader(stream))
        cls.ready, cls.review, cls.removed = clean_rows(cls.source)
        cls.by_source = {row["row_id"]: row for row in cls.source}

    def test_every_original_row_accounted_for_and_prices_categories_preserved(self):
        output = self.ready + self.review
        removed_ids = {entry["removed_row_id"] for entry in self.removed}
        self.assertEqual(len(output) + len(self.removed), 1337)
        self.assertEqual({r["row_id"] for r in output} | removed_ids, set(self.by_source))
        self.assertEqual(len({r["row_id"] for r in output}), len(output))
        for row in output:
            original = self.by_source[row["row_id"]]
            for field in ("category", "product_name", "size", "unit", "price_ksh", "raw_price", "source_name", "source_page", "source_line_start", "source_line_end"):
                self.assertEqual(row[field], original[field])
            for field in ("category", "product_name", "size", "unit", "price_ksh"):
                self.assertEqual(row[f"original_{field}"], original[field])
        self.assertEqual(len({r["category"] for r in output}), 44)

    def test_conflicts_use_explicit_name_candidate_but_remain_pending(self):
        row = next(r for r in self.review if r["source_name"] == "Gordon's Pink Can 330ml")
        self.assertEqual(row["proposed_size"], "330ml")
        self.assertEqual(row["original_unit"], "1 Litre")
        self.assertEqual(row["raw_size_column"], "1 Litre")
        self.assertEqual(row["review_status"], "pending")
        self.assertTrue(row["review_reasons"])

    def test_missing_and_ocr_sizes_are_never_invented(self):
        for name in ("Fireball Cinnamon IL", "White Cap Crisp", "Moet & Chandon Rose Imperial Brut"):
            row = next(r for r in self.review if r["source_name"] == name)
            self.assertEqual(row["size"], "")
            self.assertEqual(row["proposed_size"], "")
            self.assertEqual(row["review_status"], "pending")

    def test_duplicate_copy_removed_but_both_originals_auditable(self):
        self.assertEqual(len(self.removed), 1)
        removal = self.removed[0]
        kept = next(r for r in self.ready if r["row_id"] == removal["retained_row_id"])
        self.assertEqual(removal["original_row"], self.by_source[removal["removed_row_id"]])
        self.assertEqual(kept["price_ksh"], "1200")
        self.assertEqual(kept["review_status"], "ready")

    def test_price_or_packaging_differences_cannot_be_deduplicated(self):
        original = next(r for r in self.source if r["source_name"] == "Tusker Lager 500ml")
        different_price = original | {"row_id": "different-price", "price_ksh": "251"}
        different_unit = original | {"row_id": "different-unit", "unit": "1 Litre", "flags": "duplicate_entry"}
        ready, review, removed = clean_rows([original, different_price, different_unit])
        self.assertEqual(len(removed), 0)
        self.assertEqual(len(ready), 0)
        self.assertEqual(len(review), 3)
        self.assertIn("duplicate_entry", review[2]["flags"])
        self.assertIn("conflicting_duplicate_price", review[0]["flags"])

    def test_flavours_colours_editions_and_packs_stay_separate(self):
        for names in (
            ("Classic Blue Playing Cards", "Classic Red Playing Cards"),
            ("Smirnoff Ice Raspberry Twist 330ml", "Smirnoff Ice Raspberry Twist 330ml 6Pack"),
            ("Chivas Regal 12 Years 750ml", "Chivas Regal 15 Years 750ml"),
            ("Eros Blueberry Ice Kit 20000 Puffs", "Eros Blueberry Ice Pod 20000 Puffs"),
        ):
            rows = [next(r for r in self.ready if r["source_name"] == name) for name in names]
            self.assertNotEqual(rows[0]["product_key"], rows[1]["product_key"])

    def test_original_category_assignments_remain(self):
        row = next(r for r in self.ready if r["source_name"] == "Double Apple Mint vape")
        self.assertEqual(row["category"], "Cigars")
        self.assertIn("category_mismatch_possible", row["original_flags"])
        self.assertIn("Retain the original source category", row["resolution_notes"])

    def test_services_and_miscellaneous_never_enter_cleaned_products(self):
        self.assertEqual(sum(r["record_type"] == "checkout_service" for r in self.review), 10)
        misc = next(r for r in self.review if r["source_name"] == "Miscellaneous Sales")
        self.assertEqual(misc["storefront_eligible"], "false")
        self.assertEqual(misc["unit"], "Meters")
        for row in self.ready:
            self.assertEqual(row["record_type"], "physical_product")
            self.assertEqual(row["flags"], "")
            self.assertEqual(row["storefront_eligible"], "true")

    def test_outputs_repeat_exactly_and_validate_for_import(self):
        with tempfile.TemporaryDirectory() as directory:
            report = prepare_cleaned_catalogue(output_dir=directory)
            path = Path(directory) / "catalogue_cleaned.csv"
            content = path.read_bytes()
            prepare_cleaned_catalogue(output_dir=directory)
            self.assertEqual(content, path.read_bytes())
            self.assertEqual(report["ready_unique_products"], 1047)
            self.assertEqual(report["ready_unique_variants"], 1193)
            _, selected, _, pending, _, _, _ = read_catalogue(path, DEFAULT_INPUT.with_name("categories_2026.csv"))
            self.assertEqual(len(selected), 1193)
            self.assertEqual(pending, [])

    def test_importer_refuses_services_even_if_marked_approved(self):
        row = next(r for r in self.review if r["record_type"] == "checkout_service")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
                writer.writeheader()
                writer.writerow(row | {"review_status": "approved", "flags": "", "record_type": "physical_product", "storefront_eligible": "true"})
            with self.assertRaisesMessage(CommandError, "service or excluded entry"):
                read_catalogue(path, DEFAULT_INPUT.with_name("categories_2026.csv"))


class CleanedCatalogueDryRunTests(TestCase):
    def test_default_dry_run_uses_cleaned_file_and_never_writes(self):
        output = io.StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("import_liquor_catalog", dry_run=True, stdout=output)
        self.assertIn('"selected_entries": 1193', output.getvalue())
        self.assertIn('"pending_entries": 0', output.getvalue())
        for query in queries:
            self.assertNotRegex(query["sql"].upper(), r"^\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|BEGIN|SAVEPOINT)")

    def test_complete_cleaned_import_rehearsal_preserves_prices_and_admin_access(self):
        # This import runs ONLY in Django's disposable test database.
        path = DEFAULT_INPUT.with_name("catalogue_cleaned.csv")
        manifest = path.with_name("categories_2026.csv")
        options = {"csv": path, "approved_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "approved_categories_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(), "stdout": io.StringIO()}
        call_command("import_liquor_catalog", **options)
        call_command("import_liquor_catalog", **options)
        self.assertEqual(Product.objects.count(), 1047)
        self.assertEqual(ProductVariant.objects.count(), 1193)
        self.assertEqual(Category.objects.count(), 44)
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        variants = {v.catalogue_key: v for v in ProductVariant.objects.select_related("product__category")}
        for row in rows:
            variant = variants[row["variant_key"]]
            self.assertEqual(variant.price, Decimal(row["price_ksh"]))
            self.assertEqual(variant.bottle_size, row["variant_label"])
            self.assertEqual(variant.unit, row["unit"])
            self.assertEqual(variant.product.category.name, row["category"])
            self.assertEqual(variant.stock_quantity, 0)
        with path.with_name("catalogue_needs_review.csv").open(newline="", encoding="utf-8") as stream:
            held_keys = {row["variant_key"] for row in csv.DictReader(stream)}
        self.assertFalse(held_keys & set(variants))
        session = self.client.session
        session["age_verified_at"] = "confirmed"
        session.save()
        for category in Category.objects.all():
            response = self.client.get(reverse("category", args=[category.slug]))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "Prices shown are for demonstration only")
        variant = next(iter(variants.values()))
        detail = self.client.get(reverse("product_detail", args=[variant.product.slug]))
        self.assertContains(detail, "Catalogue price in KSh")
        self.assertNotContains(detail, "Demonstration price only")
        self.assertContains(self.client.get(reverse("home")), "Prices are from the Liquor Hub DIANI catalogue")
        user = get_user_model().objects.create_superuser("catalogue-review-test", "test@example.test", "test-password")
        self.client.force_login(user)
        changelist = self.client.get(reverse("admin:store_product_changelist"))
        self.assertEqual(changelist.status_code, 200)
        change = self.client.get(reverse("admin:store_product_change", args=[variant.product_id]))
        self.assertEqual(change.status_code, 200)
        self.assertContains(change, variant.sku)
        self.assertContains(change, "stock_quantity")
