import csv
import hashlib
import json
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from store.image_finder.discovery import discover_product
from store.image_finder.drinksvine import (PERMISSION, DrinksVineProvider, SourceClient,
    discover_urls, extract_cached_product_pages, extract_product, select_pages, stage_image)
from store.models import ImageCandidate, ImageSearchRun, ImageSource, Product, StagedSourceImage


def select_products(limit, product_ids=None):
    products = Product.objects.filter(is_active=True, category__is_active=True, catalogue_key__isnull=False, image="").exclude(variants__image__gt="")
    if product_ids:
        # Explicit IDs are the recovery path for a previous attempt.
        products = products.filter(pk__in=product_ids)
    else:
        # Any existing candidate is already available for human review. Also
        # skip attempted products that yielded no candidate so default runs
        # advance instead of repeating the same unmatched cohort forever.
        products = products.filter(image_candidates__isnull=True)
        processed_ids = set()
        prior_runs = ImageSearchRun.objects.filter(provider="drinksvine").values("statistics", "errors")
        for prior in prior_runs:
            stats = prior["statistics"]
            # Failed top-level discovery is resumable: it did not actually
            # process the selected products and must not consume their slot.
            if stats.get("source_product_urls_discovered", 0) and not prior["errors"]:
                processed_ids.update(stats.get("product_ids", []))
        if processed_ids:
            products = products.exclude(pk__in=processed_ids)
    return list(products.select_related("category").prefetch_related("variants", "image_candidates").order_by("name", "pk").distinct()[:limit])


class Command(BaseCommand):
    help = "Stage permitted Drinks Vine images and propose matches. Never approve or publish."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--product-ids", nargs="+", type=int)
        parser.add_argument("--approved-batch", action="store_true", help="Operator confirms approval for a batch beyond the 20-product pilot")
        parser.add_argument("--max-pages", type=int, default=40, help="Maximum source product pages")
        parser.add_argument("--listing-pages", type=int, default=8)
        parser.add_argument("--max-requests", type=int, default=150, help="Includes robots, metadata, images and retries")
        parser.add_argument("--output", type=Path, default=Path("catalogue_data/drinksvine_pilot"))
        parser.add_argument("--cache", type=Path, default=Path(".cache/drinksvine"))
        parser.add_argument("--cache-only", action="store_true", help="Extract source-attributed image URL leads from verified local cache files without network access or database writes")

    def handle(self, *args, **options):
        if options["cache_only"]:
            cached = extract_cached_product_pages(options["cache"])
            output = options["output"]
            output.mkdir(parents=True, exist_ok=True)
            rows = []
            assets = {asset.image_url: asset for asset in StagedSourceImage.objects.prefetch_related("candidates__product", "candidates__variant")}
            validated = 0
            for source in cached["products"]:
                for image in source["images"]:
                    asset = assets.get(image["url"])
                    staged = {"staged_record_id": asset.pk if asset else "", "staged_file": "", "staged_file_exists": False, "staged_image_valid": False,
                        "staged_format": "", "staged_width": "", "staged_height": "", "staged_bytes": "",
                        "staged_sha256": "", "staged_hash_matches_record": False, "staged_created_at": "",
                        "candidate_ids": "", "candidate_products": "", "identity_statuses": "", "rights_statuses": ""}
                    if asset:
                        staged["staged_created_at"] = asset.created_at.isoformat()
                        try:
                            local_path = Path(asset.image.path)
                            raw = local_path.read_bytes()
                            digest = hashlib.sha256(raw).hexdigest()
                            with Image.open(local_path) as probe:
                                probe.verify()
                            with Image.open(local_path) as opened:
                                staged.update({"staged_file": str(local_path), "staged_file_exists": True,
                                    "staged_image_valid": True, "staged_format": opened.format or "",
                                    "staged_width": opened.width, "staged_height": opened.height,
                                    "staged_bytes": len(raw), "staged_sha256": digest,
                                    "staged_hash_matches_record": digest == asset.sha256})
                                validated += 1
                        except (OSError, ValueError, UnidentifiedImageError):
                            staged["staged_file"] = asset.image.name
                        candidates = list(asset.candidates.all())
                        staged.update({"candidate_ids": "; ".join(str(candidate.pk) for candidate in candidates),
                            "candidate_products": "; ".join(str(candidate.product) for candidate in candidates),
                            "identity_statuses": "; ".join(candidate.match_status for candidate in candidates),
                            "rights_statuses": "; ".join(candidate.rights_status for candidate in candidates)})
                    rows.append({"source_product": source["name"], "source_brand": source["brand"],
                        "source_sizes": "; ".join(image["sizes"]), "source_role": image["role"],
                        "source_page_url": source["source_url"], "image_url": image["url"],
                        "candidate_status": "DISCOVERED; identity and rights unverified",
                        "commercial_reuse_permission": "NOT CONFIRMED", "download_permission": "NOT CONFIRMED",
                        "download_status": "NOT_DOWNLOADED; cache-only mode",
                        "attribution": f"Drinks Vine product page: {source['source_url']}", **staged})
            path = output / "drinksvine_cached_image_leads.csv"
            fields = ["source_product", "source_brand", "source_sizes", "source_role", "source_page_url", "image_url",
                "candidate_status", "commercial_reuse_permission", "download_permission", "download_status", "attribution",
                "staged_record_id", "staged_file", "staged_file_exists", "staged_image_valid", "staged_format", "staged_width", "staged_height",
                "staged_bytes", "staged_sha256", "staged_hash_matches_record", "staged_created_at", "candidate_ids",
                "candidate_products", "identity_statuses", "rights_statuses"]
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            report = {"cache_only": True, "html_pages_examined": cached["html_pages_examined"],
                "product_pages_parsed": len(cached["products"]), "direct_image_url_leads": len(rows),
                "downloads": 0, "database_writes": 0, "matching_staged_assets": sum(bool(assets.get(row["image_url"])) for row in rows),
                "locally_valid_staged_assets": validated, "browser_bytes_acquired": 0,
                "approval_or_publication": 0, "errors": cached["errors"], "output": str(path)}
            (output / "drinksvine_cache_only_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False))
            return
        if not 1 <= options["limit"] <= (100 if options["approved_batch"] else 20):
            raise CommandError("Pilot limit is 1–20; approved subsequent batches support up to 100.")
        if not 1 <= options["max_pages"] <= 200 or not 1 <= options["listing_pages"] <= 50 or not 1 <= options["max_requests"] <= 1000:
            raise CommandError("Invalid page/request bounds (pages 1–200, listings 1–50, requests 1–1000).")
        products = select_products(options["limit"], options["product_ids"])
        output = options["output"]
        output.mkdir(parents=True, exist_ok=True)
        client = SourceClient(options["cache"], max_requests=options["max_requests"])
        run = ImageSearchRun.objects.create(provider="drinksvine", options={"product_ids": [p.pk for p in products], "limit": options["limit"], "permission": PERMISSION})
        report = {"run_id": run.pk, "product_ids": [p.pk for p in products], "catalogue_products_tested": len(products),
            "source_product_urls_discovered": 0, "source_products_discovered": 0, "images_downloaded": 0,
            "staged_assets_reused": 0, "exact_product_matches": 0, "possible_matches": 0,
            "unmatched_products": 0, "products_not_processed": 0, "images_published": 0, "errors": [], "permission": PERMISSION}
        assets, source_records = {}, []
        try:
            if not products:
                raise CommandError("No eligible catalogue products without images were selected.")
            urls, errors, truncated = discover_urls(client, options["listing_pages"])
            report.update(source_product_urls_discovered=len(urls), listing_scan_bounded=truncated)
            report["errors"].extend(errors)
            if not urls and errors:
                report["products_not_processed"] = len(products)
                raise ValueError("Source discovery failed before any product could be processed; this batch remains eligible for retry.")
            for url in select_pages(products, urls, options["max_pages"]):
                try:
                    body, _, _ = client.get(url)
                    source = extract_product(body.decode("utf8", "replace"), url)
                    source_records.append(source)
                    report["source_products_discovered"] += 1
                    for image in source["images"]:
                        try:
                            asset, downloaded = stage_image(client, source, image)
                            assets[asset.pk] = asset
                            report["images_downloaded"] += int(downloaded)
                            report["staged_assets_reused"] += int(not downloaded)
                        except ValueError as error:
                            report["errors"].append({"image_url": image["url"], "error": str(error)})
                            if getattr(error, "fatal", False):
                                raise
                except ValueError as error:
                    report["errors"].append({"source_url": url, "error": str(error)})
                    if getattr(error, "fatal", False):
                        break
            provider = DrinksVineProvider(list(assets.values()))
            # Source trust helps ranking only; this never verifies candidate rights.
            source, _ = ImageSource.objects.get_or_create(domain="drinksvine.co.ke", defaults={"name": "Drinks Vine", "kind": "retailer", "verification_evidence": "Source-specific extraction from the Drinks Vine product website. " + PERMISSION})
            rows = []
            for product in products:
                discover_product(product, provider, [source], run, max_queries=1)
                candidates = list(ImageCandidate.objects.filter(product=product, provider="drinksvine"))
                product_exact = False
                for candidate in candidates:
                    asset = assets.get(candidate.metadata.get("staged_asset_id"))
                    if not asset:
                        continue
                    if not candidate.staged_image_id:
                        candidate.staged_image = asset
                        candidate.save(update_fields=["staged_image"])
                    exact = bool(candidate.metadata.get("normalized_identity_exact") and candidate.variant_id
                                 and not candidate.metadata.get("shared_size_image")
                                 and candidate.match_checks.get("size") == "matched"
                                 and candidate.match_checks.get("packaging") != "conflict")
                    queue = "exact" if exact else "possible"
                    if asset.queue != "exact":
                        asset.queue = queue
                        asset.save(update_fields=["queue"])
                    product_exact |= exact
                    rows.append({"product_id": product.pk, "variant_id": candidate.variant_id or "", "catalogue_product_name": product.name,
                        "source_product_name": asset.source_product_name, "source_url": asset.source_url,
                        "image_filename": asset.image.name, "match_confidence": candidate.confidence,
                        "review_status": candidate.match_status, "match_class": queue, "candidate_id": candidate.pk,
                        "source_sizes": "; ".join(asset.available_sizes),
                        "review_reason": "Shared image across sizes: verify bottle shown before choosing target" if candidate.metadata.get("shared_size_image") else "Human identity, edition, packaging and size confirmation required"})
                if product_exact:
                    report["exact_product_matches"] += 1
                elif candidates:
                    report["possible_matches"] += 1
                else:
                    report["unmatched_products"] += 1
                    rows.append({"product_id": product.pk, "catalogue_product_name": product.name, "match_class": "unmatched", "review_status": "unmatched", "review_reason": "No candidate in this bounded source scan"})
            fields = ["product_id", "variant_id", "catalogue_product_name", "source_product_name", "source_url", "image_filename", "match_confidence", "review_status", "match_class", "candidate_id", "source_sizes", "review_reason"]
            def write_csv(filename, records, columns):
                with (output / filename).open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(records)
            write_csv("drinksvine_image_matches.csv", rows, fields)
            write_csv("drinksvine_uncertain_matches.csv", [r for r in rows if r["match_class"] == "possible"], fields)
            write_csv("drinksvine_unmatched_products.csv", [r for r in rows if r["match_class"] == "unmatched"], fields)
            unmatched = [a for a in assets.values() if not a.candidates.exists()]
            write_csv("drinksvine_unmatched_images.csv", [{"asset_id": a.pk, "source_product_name": a.source_product_name, "source_url": a.source_url, "image_filename": a.image.name} for a in unmatched], ["asset_id", "source_product_name", "source_url", "image_filename"])
            report["unmatched_source_images"] = len(unmatched)
            report["candidates_in_review"] = len([r for r in rows if r.get("candidate_id")])
            (output / "source_products.json").write_text(json.dumps(source_records, indent=2, ensure_ascii=False) + "\n")
        except (ValueError, OSError) as error:
            report["errors"].append({"error": str(error)})
        finally:
            report["requests_sent"], report["cache_hits"] = client.requests, client.cache_hits
            report["products_still_missing_images"] = Product.objects.filter(is_active=True, catalogue_key__isnull=False, image="").exclude(variants__image__gt="").distinct().count()
            run.finished_at, run.statistics, run.errors = timezone.now(), {k: v for k, v in report.items() if k != "errors"}, report["errors"]
            run.save(update_fields=["finished_at", "statistics", "errors"])
            (output / "drinksvine_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False))
        if report["errors"]:
            raise CommandError("Extraction completed with errors; inspect drinksvine_report.json before retrying.")
