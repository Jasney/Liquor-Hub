import csv
import importlib.util
import shutil
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .image_finder.catalogue_audit import rank_match, workbook_products


class CatalogueImageAuditCoreTests(SimpleTestCase):
    def test_reads_all_products_worksheet_and_expected_fields(self):
        products = workbook_products(Path("media/Liquorhub_Website_Catalogue_and_Image_Sourcing.xlsx"))
        self.assertEqual(len(products), 1040)
        self.assertEqual(products[0]["name"], "Atlas 10% 500ml")
        self.assertEqual(products[0]["barcode"], "1418")
        self.assertEqual(products[0]["filename"], "atlas-10-500ml.jpg")

    def test_wrong_size_is_a_conflict(self):
        product = {"name": "4th Street Sweet Red 5L", "size": "1 Piece"}
        score, size_state, _, _ = rank_match(product, "4th street red sweet", ["750 ml", "1.5 litres"])
        self.assertEqual(size_state, "conflict")
        self.assertLessEqual(score, 45)

    def test_pack_quantity_mismatch_is_a_conflict(self):
        product = {"name": "Corona 330ml 6 Pack", "size": "Piece"}
        score, _, pack_state, _ = rank_match(product, "Corona Extra 330ml 12 Pack")
        self.assertEqual(pack_state, "conflict")
        self.assertLessEqual(score, 35)

    def test_source_progress_tracker_covers_catalogue_without_approving(self):
        root = Path(__file__).resolve().parents[1]
        runner_path = root / "image_research_batch_01" / "run_offline_audit.py"
        spec = importlib.util.spec_from_file_location("offline_catalogue_audit", runner_path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        research_output = root / "catalogue_data" / "image_sourcing"
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            for name in ("first_nine_source_leads.csv", "catalogue_image_best_matches.csv"):
                shutil.copy2(research_output / name, output / name)
            summary = runner.build_source_progress(
                root / "media" / "Liquorhub_Website_Catalogue_and_Image_Sourcing.xlsx",
                output, root / "image_research_batch_01")
            with (output / "catalogue_source_progress_1040.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1040)
        self.assertEqual(summary["staged_candidate_products"], 4)
        self.assertEqual(summary["identity_verified"], 0)
        self.assertEqual(summary["approved"], 0)

