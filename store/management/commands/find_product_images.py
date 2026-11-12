import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from store.image_finder.discovery import discover_product
from store.image_finder.matching import build_queries
from store.image_finder.providers import DiscoveryError, FeedProvider
from store.models import ImageCandidate, ImageSearchRun, ImageSource, Product


class Command(BaseCommand):
    help = "Find up to five review candidates per product from a permitted JSON supplier feed. Never downloads originals or publishes images."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Plan queries only: no API calls, downloads or database writes")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--category", help="Exact category name or URL slug")
        parser.add_argument("--missing-only", action="store_true", default=True, help="Only products without a displayed image (default)")
        parser.add_argument("--include-existing", action="store_false", dest="missing_only", help="Also discover candidates for already illustrated products; does not replace their images")
        parser.add_argument("--feed", type=Path, help="Local supplier JSON feed with documented discovery permission")
        parser.add_argument("--report", type=Path, help="Write the complete JSON report here")
        parser.add_argument("--catalogue-only", action="store_true", help="Only products imported from the approved catalogue")
        parser.add_argument("--product-ids", nargs="+", type=int, help="Restrict the run to an explicitly selected pilot cohort")
        parser.add_argument("--max-queries-per-product", type=int, default=6, help="Maximum distinct queries per product (1–6)")

    def handle(self, *args, **options):
        if options["limit"] < 1 or options["limit"] > 1000:
            raise CommandError("--limit must be between 1 and 1000.")
        if not 1 <= options["max_queries_per_product"] <= 6:
            raise CommandError("--max-queries-per-product must be 1–6.")
        if not options["dry_run"] and not options["feed"]:
            raise CommandError("Supply --feed with a permitted supplier JSON feed. No online source adapter is configured.")
        products = Product.objects.filter(is_active=True, category__is_active=True)
        if options["catalogue_only"]:
            products = products.filter(catalogue_key__isnull=False)
        if options["product_ids"]:
            products = products.filter(pk__in=options["product_ids"])
        if options["category"]:
            products = products.filter(Q(category__slug__iexact=options["category"]) | Q(category__name__iexact=options["category"]))
        if options["missing_only"]:
            products = products.filter(image="").exclude(variants__image__gt="")
        products = list(products.select_related("category").prefetch_related("variants", "image_candidates").distinct()[:options["limit"]])
        sources = list(ImageSource.objects.filter(is_active=True))
        summary = {
            "mode": "dry_run" if options["dry_run"] else "discovery_only",
            "provider": "feed" if options["feed"] else "unconfigured", "products_selected": len(products),
            "products_processed": 0, "queries": 0, "candidates_found": 0, "candidates_created": 0,
            "products_without_candidates": 0, "images_downloaded": 0, "images_published": 0,
            "errors": [], "products": [],
            "max_queries_per_product": options["max_queries_per_product"],
            "deferred_product_ids": [],
        }
        if options["dry_run"]:
            for product in products:
                queries = build_queries(product, sources, maximum=options["max_queries_per_product"])
                summary["products"].append({"product_id": product.pk, "name": product.name, "category": product.category.name,
                                            "sizes": [v.bottle_size for v in product.variants.all()], "queries": queries})
                summary["queries"] += len(queries)
        else:
            try:
                provider = FeedProvider(options["feed"])
            except DiscoveryError as error:
                raise CommandError(str(error)) from error
            run = ImageSearchRun.objects.create(provider=summary["provider"], options={"limit": options["limit"], "category": options["category"], "missing_only": options["missing_only"], "catalogue_only": options["catalogue_only"], "product_ids": [p.pk for p in products], "max_queries_per_product": options["max_queries_per_product"]})
            for index, product in enumerate(products):
                try:
                    stats = discover_product(product, provider, sources, run, max_queries=options["max_queries_per_product"])
                    summary["products_processed"] += 1
                    summary["queries"] += stats["queries"]
                    summary["candidates_found"] += stats["found"]
                    summary["candidates_created"] += stats["created"]
                    summary["products_without_candidates"] += int(not (stats["created"] or stats["already_present"]))
                    candidates_for_product = list(product.image_candidates.order_by("-confidence", "pk"))
                    summary["products"].append({"product_id": product.pk, "name": product.name, **stats,
                        "candidates": [{"candidate_id": c.pk, "title": c.title, "source_url": c.source_url,
                                        "image_url": c.image_url, "confidence": c.confidence,
                                        "match_status": c.match_status, "rights_status": c.rights_status,
                                        "variant_id": c.variant_id} for c in candidates_for_product]})
                    self.stderr.write(f"{product.name}: {stats['created']} new candidates; no original images downloaded.")
                except (DiscoveryError, ValueError) as error:
                    summary["errors"].append({"product_id": product.pk, "name": product.name, "error": str(error)})
                    if getattr(error, "fatal", False):
                        summary["deferred_product_ids"] = [p.pk for p in products[index + 1:]]
                        break
            # State summaries are separate: match approval never counts as rights
            # approval or publication.
            candidates = ImageCandidate.objects.filter(product__in=products)
            summary["review_state"] = {
                "pending": candidates.filter(match_status="pending").count(),
                "match_approved": candidates.filter(match_status="approved").count(),
                "rejected": candidates.filter(match_status="rejected").count(),
                "skipped": candidates.filter(match_status="skipped").count(),
                "rights_verified": candidates.filter(rights_status="verified").count(),
                "already_published": candidates.filter(published_at__isnull=False).count(),
            }
            run.finished_at = timezone.now()
            run.statistics = {key: value for key, value in summary.items() if key not in {"products", "errors"}}
            run.errors = summary["errors"]
            run.save(update_fields=["finished_at", "statistics", "errors"])
        output = json.dumps(summary, indent=2, ensure_ascii=False)
        self.stdout.write(output)
        if options["report"]:
            options["report"].write_text(output + "\n", encoding="utf-8")
        if summary["errors"]:
            raise CommandError("Some products could not be searched; see the report. No images were downloaded or published.")
