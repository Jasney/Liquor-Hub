"""Audit staged image candidates against the supplied 1,040-item catalogue workbook.

This command is read-only with respect to the Django database and product media.
It validates staged files, ranks metadata matches, and writes review reports only.
"""
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from PIL import Image, UnidentifiedImageError
from store.image_finder.catalogue_audit import multipacks, normalized, normalized_size_values, rank_match, tokens, workbook_products


DEFAULT_WORKBOOK = Path("media/Liquorhub_Website_Catalogue_and_Image_Sourcing.xlsx")
DEFAULT_OUTPUT = Path("catalogue_data/image_sourcing")
DEFAULT_STAGING = Path("media/image-staging")
DEFAULT_RESEARCH = Path("image_research_batch_01")
DEFAULT_SOURCE = "https://drinksvine.co.ke/"


def metadata_csv(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


class Command(BaseCommand):
    help = "Create a read-only catalogue image matching, file validation, and sourcing audit."

    def add_arguments(self, parser):
        parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
        parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        parser.add_argument("--staging", type=Path, default=DEFAULT_STAGING)
        parser.add_argument("--research", type=Path, default=DEFAULT_RESEARCH)
        parser.add_argument("--minimum-confidence", type=int, default=65)

    def handle(self, *args, **options):
        workbook = options["workbook"]
        if not workbook.is_file():
            raise CommandError(f"Catalogue workbook not found: {workbook}")
        products = workbook_products(workbook)
        if not products:
            raise CommandError("No catalogue products found in the All Products worksheet.")

        research = options["research"]
        pilot_dir = research.parent / "catalogue_data" / "drinksvine_pilot"
        match_rows = metadata_csv(pilot_dir / "drinksvine_image_matches.csv")
        prior_sources = {row.get("ProductName", "").strip().casefold(): row
                         for row in metadata_csv(research / "image_research_report.csv")}
        staged_records = {}
        for row in match_rows:
            if not row.get("image_filename"):
                continue
            key = Path(row["image_filename"]).name
            staged_records[key] = {
                "source_name": row.get("source_product_name", ""),
                "source_url": row.get("source_url", ""),
                "rights": "unresolved; source-owner permission claim requires administrator evidence review",
                "rights_evidence": "",
                "available_sizes": [s.strip() for s in row.get("source_sizes", "").split(";") if s.strip()],
                "source_type": "Drinks Vine pilot",
            }
        for row in metadata_csv(research / "automated_image_sourcing_report_final.csv"):
            filename = Path(row.get("SavedFilename", "")).name
            if filename:
                staged_records[filename] = {
                    "source_name": row.get("ProductName", ""),
                    "source_url": row.get("ProductPageURL", "") or row.get("ImageSourceURL", ""),
                    "rights": row.get("UsageRightsStatus", "unconfirmed"),
                    "rights_evidence": row.get("RightsEvidenceURL", ""),
                    "available_sizes": [], "source_type": "prior catalogue candidate report",
                }
        # The staged Drinks Vine directory is authoritative for actual present files.
        staging = options["staging"]
        files = sorted(p for p in staging.rglob("*") if p.is_file()) if staging.exists() else []
        for file in files:
            staged_records.setdefault(file.name, {
                "source_name": file.stem.replace("-", " "), "source_url": "",
                "rights": "unresolved; no rights evidence recorded for local file",
                "rights_evidence": "", "available_sizes": [], "source_type": "unindexed staged file",
            })
        # Include separately staged catalogue candidates outside media/image-staging.
        candidate_dir = research / "candidates"
        if candidate_dir.exists():
            files.extend(sorted(p for p in candidate_dir.iterdir() if p.is_file()))
            for file in candidate_dir.iterdir():
                if file.is_file():
                    staged_records.setdefault(file.name, {
                        "source_name": "Atlas 16% 500ml" if "atlas-16" in file.name else file.stem.replace("-", " "),
                        "source_url": "https://www.carrefour.ke/mafken/en/beer/atlas-beer-16-can-500ml/p/122514" if "atlas-16" in file.name else "",
                        "rights": "unconfirmed; commercial reuse permission not established",
                        "rights_evidence": "", "available_sizes": [], "source_type": "manual research candidate",
                    })

        file_info, hashes = {}, defaultdict(list)
        catalogue_by_filename = defaultdict(list)
        for product in products:
            if product["filename"]:
                catalogue_by_filename[product["filename"].casefold()].append(product)
        invalid = []
        for file in files:
            try:
                digest = hashlib.sha256(file.read_bytes()).hexdigest()
                with Image.open(file) as image:
                    image_format, width, height = image.format, *image.size
                    frames = getattr(image, "n_frames", 1)
                    image.verify()
                file_info[str(file)] = {"format": image_format, "width": width, "height": height,
                                        "frames": frames, "sha256": digest, "bytes": file.stat().st_size,
                                        "valid": frames == 1}
                hashes[digest].append(str(file))
                if frames != 1:
                    invalid.append({"path": str(file), "reason": f"animated/multi-frame image ({frames} frames)"})
            except (OSError, UnidentifiedImageError, ValueError) as error:
                file_info[str(file)] = {"valid": False, "error": f"{type(error).__name__}: {error}"}
                invalid.append({"path": str(file), "reason": file_info[str(file)]["error"]})

        path_by_name = defaultdict(list)
        for file in files:
            path_by_name[file.name].append(file)
        match_results = []
        best_by_product = {}
        candidates_by_product = defaultdict(list)
        for product in products:
            for filename, sources in path_by_name.items():
                source = staged_records.get(filename, {})
                source_name = source.get("source_name") or Path(filename).stem.replace("-", " ")
                score, size_state, pack_state, rationale = rank_match(product, source_name, source.get("available_sizes", []))
                info = file_info.get(str(sources[0]), {"valid": False})
                filename_match = product["filename"].casefold() == filename.casefold()
                if filename_match:
                    rationale += "; filename matches workbook expectation (not identity proof)"
                # Identical optimized copies in separate source paths count as one match candidate.
                confidence = score
                if size_state == "unknown" and normalized_size_values(product["name"] + " " + product["size"]):
                    confidence = min(confidence, 60)
                    rationale += "; catalogue size not verified by source metadata"
                if len(source.get("available_sizes", [])) > 1:
                    confidence = min(confidence, 60)
                    rationale += "; source image shared across multiple sizes"
                row = {"catalogue_row": product["catalogue_row"], "product_name": product["name"],
                       "category": product["category"], "catalogue_size": product["size"],
                       "catalogue_filename": product["filename"], "candidate_image_path": str(sources[0]),
                       "source_image_paths": "; ".join(str(p) for p in sources),
                       "source_product_name": source_name, "source_url": source.get("source_url", ""),
                       "source_type": source.get("source_type", "staged local asset"),
                       "confidence_metadata_only": confidence, "size_match": size_state,
                       "multipack_match": pack_state, "match_reason": rationale,
                       "file_valid": info.get("valid", False), "format": info.get("format", ""),
                       "dimensions": f"{info.get('width', '')}x{info.get('height', '')}" if info.get("width") else "",
                       "bytes": info.get("bytes", ""), "sha256": info.get("sha256", ""),
                       "rights_status": source.get("rights", "unresolved"),
                       "rights_evidence_url": source.get("rights_evidence", ""),
                       "manual_review_status": "pending identity, packaging, size, visual, and rights review"}
                candidates_by_product[product["catalogue_row"]].append(row)
            ranked = sorted(candidates_by_product[product["catalogue_row"]],
                            key=lambda r: (-r["confidence_metadata_only"], r["candidate_image_path"]))
            best_by_product[product["catalogue_row"]] = ranked

        threshold = options["minimum_confidence"]
        best_rows, all_rows, missing_rows = [], [], []
        low_confidence_rows = []
        for product in products:
            ranked = best_by_product[product["catalogue_row"]]
            suitable = [r for r in ranked if r["file_valid"] and r["confidence_metadata_only"] >= threshold
                        and r["size_match"] != "conflict" and r["multipack_match"] != "conflict"]
            all_rows.extend(ranked)
            if suitable:
                best_rows.append(suitable[0])
            else:
                if ranked:
                    low_confidence_rows.append(ranked[0])
                best_rows.append({"catalogue_row": product["catalogue_row"], "product_name": product["name"],
                                  "category": product["category"], "catalogue_size": product["size"],
                                  "catalogue_filename": product["filename"], "candidate_image_path": "",
                                  "source_product_name": "", "source_url": "", "source_type": "",
                                  "confidence_metadata_only": 0, "size_match": "unknown", "multipack_match": "unknown",
                                  "match_reason": "No staged image met the metadata threshold without size/multipack conflict.",
                                  "file_valid": False, "format": "", "dimensions": "", "bytes": "", "sha256": "",
                                  "rights_status": "unresolved", "rights_evidence_url": "",
                                  "manual_review_status": "missing; sourcing required"})
                candidates = ranked[:3]
                source_lead = prior_sources.get(product["name"].casefold(), {})
                source_page = source_lead.get("ProductPageURL", "").strip()
                source_image = source_lead.get("ImageSourceURL", "").strip()
                has_lead = bool(source_page or source_image)
                lead_status = "source lead identified; verify accessibility and image rights" if has_lead else "no vetted source page or direct image URL recorded"
                missing_rows.append({"catalogue_row": product["catalogue_row"], "product_name": product["name"],
                                     "category": product["category"], "size": product["size"],
                                     "catalogue_filename": product["filename"],
                                     "best_low_confidence_candidate": candidates[0]["candidate_image_path"] if candidates else "",
                                     "best_candidate_name": candidates[0]["source_product_name"] if candidates else "",
                                     "best_candidate_confidence": candidates[0]["confidence_metadata_only"] if candidates else 0,
                                     "reason": candidates[0]["match_reason"] if candidates else "No staged image candidates.",
                                     "source_page_url": source_page, "image_source_url": source_image,
                                     "rights_status": source_lead.get("UsageRightsStatus", "unconfirmed"),
                                     "priority": "source lead available" if has_lead else "source research needed",
                                     "sourcing_status": lead_status})

        rights_unknown = sum(1 for row in best_rows if row.get("candidate_image_path") and
                             ("unconfirm" in row.get("rights_status", "").casefold() or "unresolved" in row.get("rights_status", "").casefold()))
        low_confidence = sum(1 for rows in best_by_product.values() if rows and rows[0].get("confidence_metadata_only", 0) < threshold)
        high_confidence = sum(1 for r in best_rows if r.get("candidate_image_path") and r.get("confidence_metadata_only", 0) >= threshold)
        duplicates = {digest: paths for digest, paths in hashes.items() if len(paths) > 1}
        summary = {
            "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
            "workbook": str(workbook), "catalogue_products": len(products), "staged_files_examined": len(files),
            "products_with_suitable_metadata_candidates": sum(bool(r.get("candidate_image_path")) for r in best_rows),
            "products_missing_suitable_staged_candidate": len(missing_rows), "high_confidence_best_matches": high_confidence,
            "low_confidence_best_matches": low_confidence,
            "invalid_files": len(invalid), "duplicate_file_groups": len(duplicates),
            "duplicate_files_beyond_first": sum(len(v) - 1 for v in duplicates.values()),
            "candidate_products_with_unresolved_rights": rights_unknown,
            "verified_candidate_images": 0,
            "note": "All automated matches are metadata-only and pending human product/visual review. No product records or live images were changed.",
            "limitations": ["No commercial reuse rights were inferred from public availability or source ownership claims.",
                            "External sourcing requires a permitted feed or product-page metadata access; Google Images result pages are not scraped."],
        }
        output = options["output"]
        output.mkdir(parents=True, exist_ok=True)

        def write_csv(name, rows):
            if not rows:
                (output / name).write_text("", encoding="utf-8")
                return
            fields = list(dict.fromkeys(key for row in rows for key in row))
            with (output / name).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)

        write_csv("catalogue_image_best_matches.csv", best_rows)
        write_csv("catalogue_image_all_candidates.csv", sorted(all_rows, key=lambda r: (r["catalogue_row"], -r["confidence_metadata_only"])))
        write_csv("catalogue_image_low_confidence.csv", low_confidence_rows)
        write_csv("catalogue_image_sourcing_priority.csv", missing_rows)
        write_csv("catalogue_image_file_validation.csv", [dict(path=path, **info) for path, info in file_info.items()])
        (output / "catalogue_image_audit.json").write_text(json.dumps({
            "summary": summary, "invalid_file_details": invalid,
            "duplicate_groups": [{"sha256": digest, "paths": paths} for digest, paths in duplicates.items()],
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        lines = ["# Catalogue image sourcing audit", "", f"Generated: {summary['generated_at']}", "",
                 f"- Catalogue products: {len(products)}", f"- Staged files examined: {len(files)}",
                 f"- Products with suitable metadata candidate: {summary['products_with_suitable_metadata_candidates']}",
                 f"- Products missing a suitable staged candidate: {len(missing_rows)}",
                 f"- High-confidence metadata matches: {high_confidence}",
                 f"- Low-confidence best matches: {low_confidence}", f"- Invalid files: {len(invalid)}",
                 f"- Duplicate file groups: {len(duplicates)} ({summary['duplicate_files_beyond_first']} duplicate copies)",
                 f"- Candidate products with unresolved rights: {rights_unknown}",
                 "- Verified candidate images: 0 (visual identity and commercial rights are not verified by automated matching)", "",
                 "All candidate matches require human identity, size, packaging and visual review. Source permission claims remain separate from verified commercial reuse rights.",
                 "No live product records or image fields were modified.", ""]
        (output / "CATALOGUE_IMAGE_AUDIT.md").write_text("\n".join(lines), encoding="utf-8")
        self.stdout.write(json.dumps(summary, indent=2))
