import hashlib
import io
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from PIL import Image

from .image_finder.discovery import discover_product
from .image_finder.matching import build_queries, public_https_url, score_candidate, snapshot
from .image_finder.network import ImageDownloadError, download_image, pinned_get, resolve_public_host
from .image_finder.providers import DiscoveryError, FeedProvider, normalize_result, retry_delay
from .image_finder.review import MATCH_CONFIRMATIONS, ReviewError, optimize_image, publish_candidate, review_match, verify_rights, withdraw_candidate
from .models import Category, ImageCandidate, ImageReviewEvent, ImageSearchRun, ImageSource, Product, ProductVariant


def photo_bytes(size=(600, 1200), fmt="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", size, "white").save(stream, format=fmt)
    return stream.getvalue()


class ProductImageData(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Gin")
        self.product = Product.objects.create(name="Acme Pink Gin", category=self.category)
        self.variant = ProductVariant.objects.create(product=self.product, bottle_size="750ml", unit="Piece", price=Decimal("1500"), stock_quantity=2)
        self.user = get_user_model().objects.create_superuser("image-owner", "owner@example.test", "test-password")

    def result(self, **overrides):
        return {"title": "Acme Pink Gin 750ml product photograph", "source_url": "https://supplier.example/acme-pink",
                "image_url": "https://cdn.supplier.example/acme-pink-750.png", "thumbnail_url": "",
                "preview_permitted": False, "width": 600, "height": 1200, "attributes": {}, "metadata": {}, **overrides}

    def candidate(self):
        result = self.result()
        score, checks, variant, source = score_candidate(self.product, result)
        return ImageCandidate.objects.create(
            product=self.product, variant=variant, provider="feed", title=result["title"], source_url=result["source_url"],
            image_url=result["image_url"], image_url_hash=hashlib.sha256(result["image_url"].encode()).hexdigest(),
            confidence=score, match_checks=checks, product_snapshot=snapshot(self.product), width=600, height=1200,
        )

    def approve(self, candidate):
        return review_match(candidate.pk, self.user, "approved", {field: True for field in MATCH_CONFIRMATIONS})

    def license(self, candidate):
        candidate.refresh_from_db()
        candidate.rights_holder = "Supplier photographer"
        candidate.license_name = "Written commercial licence"
        candidate.rights_evidence = "Agreement A-1 explicitly permits commercial display and downloading this exact photograph."
        candidate.commercial_reuse_confirmed = True
        candidate.download_permission_confirmed = True
        candidate.attribution_required = True
        candidate.attribution_text = "Photo: Supplier photographer"
        candidate.attribution_url = "https://supplier.example/photographer"
        candidate.save()
        return verify_rights(candidate.pk, self.user)


class ImageMatchingTests(ProductImageData):
    def test_query_uses_name_size_category_and_preferred_source(self):
        source = ImageSource.objects.create(name="Acme manufacturer", domain="acme.example", kind="manufacturer", brand_keywords="Acme", verification_evidence="Verified manufacturer's published domain")
        queries = build_queries(self.product, [source])
        self.assertIn('"Acme Pink Gin"', queries[0])
        self.assertIn("750ml", queries[0])
        self.assertIn("site:acme.example", queries[0])

    def test_filename_cannot_establish_product_match(self):
        correct = score_candidate(self.product, self.result())[0]
        wrong = score_candidate(self.product, self.result(title="Unrelated Blue Whisky", image_url="https://cdn.supplier.example/Acme-Pink-Gin-750ml.png"))[0]
        self.assertLess(wrong, correct)
        self.assertLessEqual(wrong, 25)

    def test_wrong_flavour_size_and_packaging_are_explicit(self):
        score, checks, _, _ = score_candidate(self.product, self.result(title="Acme Orange Gin 1L 6 pack"))
        self.assertIn("pink", checks["missing_identity_tokens"])
        self.assertEqual(checks["size"], "conflict")
        self.assertEqual(checks["packaging"], "conflict")
        self.assertLessEqual(score, 35)

    def test_source_priority_never_implies_rights(self):
        source = ImageSource.objects.create(name="Acme", domain="supplier.example", kind="manufacturer", brand_keywords="Acme", verification_evidence="Verified domain")
        provider = Mock(name="provider")
        provider.name = "feed"
        provider.search.return_value = [self.result()]
        discover_product(self.product, provider, [source])
        candidate = ImageCandidate.objects.get()
        self.assertEqual(candidate.source, source)
        self.assertEqual(candidate.match_status, "pending")
        self.assertEqual(candidate.rights_status, "unknown")
        self.assertFalse(candidate.stored_image)

    def test_new_source_adapter_cannot_bypass_preview_domain_or_reviews(self):
        provider = Mock()
        provider.name = "future_supplier"
        provider.search.return_value = [self.result(
            thumbnail_url="https://unverified.example/preview.png", preview_permitted=True)]
        discover_product(self.product, provider, [])
        candidate = ImageCandidate.objects.get()
        self.assertEqual(candidate.provider, "future_supplier")
        self.assertFalse(candidate.preview_permitted)
        self.assertEqual(candidate.match_status, "pending")
        self.assertEqual(candidate.rights_status, "unknown")
        self.assertFalse(candidate.stored_image)

    def test_discovery_caps_at_five_and_repeats_preserve_rejection(self):
        provider = Mock()
        provider.name = "feed"
        provider.search.return_value = [self.result(image_url=f"https://supplier.example/{i}.png") for i in range(8)]
        discover_product(self.product, provider, [])
        self.assertEqual(ImageCandidate.objects.count(), 5)
        first = ImageCandidate.objects.first()
        review_match(first.pk, self.user, "rejected")
        discover_product(self.product, provider, [])
        self.assertEqual(ImageCandidate.objects.count(), 5)
        first.refresh_from_db()
        self.assertEqual(first.match_status, "rejected")

    def test_discovery_stops_after_five_strong_candidates(self):
        ProductVariant.objects.create(product=self.product, bottle_size="350ml", price=Decimal("800"))
        provider = Mock()
        provider.name = "feed"
        provider.search.return_value = [self.result(image_url=f"https://supplier.example/exact-{i}.png") for i in range(5)]
        stats = discover_product(self.product, provider, [])
        self.assertEqual(stats["created"], 5)
        self.assertEqual(stats["queries"], 1)
        provider.search.assert_called_once()


class DiscoveryCommandTests(ProductImageData):
    def test_dry_run_can_freeze_imported_cohort_and_query_limit(self):
        self.product.catalogue_key = "test-catalogue-key"
        self.product.save()
        Product.objects.create(name="Another active product", category=self.category)
        ProductVariant.objects.create(product=self.product,bottle_size="350ml",price=Decimal("800"))
        output=io.StringIO()
        call_command("find_product_images",dry_run=True,catalogue_only=True,product_ids=[self.product.pk],max_queries_per_product=1,stdout=output)
        report=json.loads(output.getvalue())
        self.assertEqual(report["products_selected"],1)
        self.assertEqual(report["products"][0]["product_id"],self.product.pk)
        self.assertEqual(report["queries"],1)
        self.assertEqual(report["provider"], "unconfigured")

    def test_dry_run_never_calls_provider_or_mutates_database(self):
        with patch("store.management.commands.find_product_images.FeedProvider") as provider, CaptureQueriesContext(connection) as queries:
            output = io.StringIO()
            call_command("find_product_images", dry_run=True, limit=20, category="gin", stdout=output)
        provider.assert_not_called()
        self.assertIn("750ml", output.getvalue())
        self.assertEqual(ImageCandidate.objects.count(), 0)
        self.assertEqual(ImageSearchRun.objects.count(), 0)
        for query in queries:
            self.assertNotRegex(query["sql"].upper(), r"^\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|BEGIN|SAVEPOINT)")

    def test_missing_only_respects_manual_product_and_variant_images(self):
        self.product.image = "products/manual.jpg"
        self.product.save()
        output = io.StringIO()
        call_command("find_product_images", dry_run=True, missing_only=True, stdout=output)
        self.assertEqual(json.loads(output.getvalue())["products_selected"], 0)
        self.product.image = ""
        self.product.save()
        self.variant.image = "products/manual-variant.jpg"
        self.variant.save()
        output = io.StringIO()
        call_command("find_product_images", dry_run=True, stdout=output)
        self.assertEqual(json.loads(output.getvalue())["products_selected"], 0)

    def test_permitted_feed_stores_metadata_and_run_report_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.json"
            path.write_text(json.dumps({"permission_reference": "Supplier discovery feed agreement", "items": [self.result()]}))
            report = Path(directory) / "report.json"
            call_command("find_product_images", feed=path, report=report, stdout=io.StringIO(), stderr=io.StringIO())
            data = json.loads(report.read_text())
        self.assertEqual(data["candidates_created"], 1)
        self.assertEqual(data["images_downloaded"], 0)
        self.assertEqual(data["images_published"], 0)
        self.assertEqual(data["products_without_candidates"], 0)
        self.assertEqual(ImageSearchRun.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertFalse(self.product.image)

    def test_unconfigured_provider_requires_explicit_source(self):
        with self.assertRaisesMessage(CommandError, "Supply --feed"):
            call_command("find_product_images", stdout=io.StringIO())
        self.assertFalse(ImageSearchRun.objects.exists())
        self.assertFalse(ImageCandidate.objects.exists())

    def test_no_ordinary_customer_request_triggers_search(self):
        session = self.client.session
        session["age_verified_at"] = "confirmed"
        session.save()
        with patch("store.image_finder.providers.FeedProvider.search") as api, patch("store.image_finder.review.download_image") as download:
            self.assertEqual(self.client.get(reverse("shop")).status_code, 200)
            self.assertEqual(self.client.get(reverse("product_detail", args=[self.product.slug])).status_code, 200)
        api.assert_not_called()
        download.assert_not_called()


class ImageReviewTests(ProductImageData):
    def test_match_approval_does_not_verify_rights_or_download(self):
        candidate = self.candidate()
        with patch("store.image_finder.review.download_image") as download:
            self.approve(candidate)
            with self.assertRaisesMessage(ReviewError, "commercial reuse"):
                publish_candidate(candidate.pk, self.user)
        candidate.refresh_from_db()
        self.assertEqual(candidate.match_status, "approved")
        self.assertEqual(candidate.rights_status, "unknown")
        download.assert_not_called()

    def test_rights_alone_and_missing_checklist_never_download(self):
        candidate = self.candidate()
        self.license(candidate)
        with patch("store.image_finder.review.download_image") as download:
            with self.assertRaisesMessage(ReviewError, "human product match"):
                publish_candidate(candidate.pk, self.user)
            with self.assertRaisesMessage(ReviewError, "Confirm identity"):
                review_match(candidate.pk, self.user, "approved", {})
        download.assert_not_called()

    def test_missing_rights_evidence_or_required_attribution_blocks_verification(self):
        candidate = self.candidate()
        with self.assertRaisesMessage(ReviewError, "rights holder"):
            verify_rights(candidate.pk, self.user)
        self.license(candidate)
        candidate.refresh_from_db()
        candidate.attribution_text = ""
        candidate.save()
        with self.assertRaises(ReviewError):
            verify_rights(candidate.pk, self.user)

    def test_stale_product_or_changed_rights_block_before_download(self):
        candidate = self.candidate()
        self.approve(candidate)
        self.license(candidate)
        self.product.name = "Acme Orange Gin"
        self.product.save()
        with patch("store.image_finder.review.download_image") as download:
            with self.assertRaisesMessage(ReviewError, "details changed"):
                publish_candidate(candidate.pk, self.user)
        download.assert_not_called()

    def test_edited_permission_evidence_must_be_reverified(self):
        candidate = self.candidate()
        self.approve(candidate)
        self.license(candidate)
        candidate.refresh_from_db()
        candidate.rights_evidence = "Changed agreement"
        candidate.save()
        with patch("store.image_finder.review.download_image") as download:
            with self.assertRaisesMessage(ReviewError, "evidence changed"):
                publish_candidate(candidate.pk, self.user)
        download.assert_not_called()

    def test_manual_images_are_preserved(self):
        candidate = self.candidate()
        self.approve(candidate)
        self.license(candidate)
        self.variant.image = "products/my-manual-image.png"
        self.variant.save()
        with patch("store.image_finder.review.download_image") as download:
            with self.assertRaisesMessage(ReviewError, "already has an image"):
                publish_candidate(candidate.pk, self.user)
        download.assert_not_called()

    def test_publish_optimizes_associates_variant_and_retains_provenance(self):
        candidate = self.candidate()
        self.approve(candidate)
        self.license(candidate)
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory), patch("store.image_finder.review.download_image", return_value=(photo_bytes(), "image/png", candidate.image_url)):
            published = publish_candidate(candidate.pk, self.user)
            self.variant.refresh_from_db()
            self.product.refresh_from_db()
            self.assertEqual(self.variant.image.name, published.stored_image.name)
            self.assertFalse(self.product.image)
            self.assertTrue(published.stored_image.name.endswith(".webp"))
            self.assertTrue(Path(published.stored_image.path).exists())
            self.assertEqual(published.download_info["width"], 600)
            self.assertEqual(published.source_url, candidate.source_url)
            self.assertEqual(published.credit_text, "Photo: Supplier photographer")
            self.assertEqual(ImageReviewEvent.objects.filter(candidate=candidate).count(), 3)
            session = self.client.session
            session["age_verified_at"] = "confirmed"
            session.save()
            self.assertContains(self.client.get(reverse("shop")), "Photo: Supplier photographer")
            self.assertContains(self.client.get(reverse("product_detail", args=[self.product.slug])), published.stored_image.url)
            with self.captureOnCommitCallbacks(execute=True):
                withdraw_candidate(candidate.pk, self.user)
            self.variant.refresh_from_db()
            self.assertFalse(self.variant.image)
            self.assertFalse(Path(published.stored_image.path).exists())

    def test_publish_error_does_not_leave_a_product_image(self):
        candidate = self.candidate()
        self.approve(candidate)
        self.license(candidate)
        with patch("store.image_finder.review.download_image", return_value=(b"not a photo", "image/png", candidate.image_url)):
            with self.assertRaises(ImageDownloadError):
                publish_candidate(candidate.pk, self.user)
        self.variant.refresh_from_db()
        self.assertFalse(self.variant.image)

    def test_admin_review_and_separate_controls_render(self):
        candidate = self.candidate()
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin:store_imagecandidate_change", args=[candidate.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Approve product match")
        self.assertContains(response, "Verify commercial reuse rights")
        self.assertContains(response, "Download and publish verified image")

    def test_staff_without_review_permission_cannot_approve(self):
        user = get_user_model().objects.create_user("image-staff", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="change_imagecandidate"), Permission.objects.get(codename="view_imagecandidate"))
        candidate = self.candidate()
        with self.assertRaisesMessage(ReviewError, "permission"):
            review_match(candidate.pk, user, "approved", {field: True for field in MATCH_CONFIRMATIONS})
        with self.assertRaisesMessage(ReviewError, "permission"):
            verify_rights(candidate.pk, user)
        with self.assertRaisesMessage(ReviewError, "permission"):
            publish_candidate(candidate.pk, user)

    def test_admin_post_workflow_keeps_matching_and_rights_separate_until_publish(self):
        candidate = self.candidate()
        self.client.force_login(self.user)
        url = reverse("admin:store_imagecandidate_change", args=[candidate.pk])

        def post(action, **fields):
            events = list(candidate.review_events.all())
            data = {"variant": self.variant.pk, "review_notes": "Visually checked source photograph",
                    "review_events-TOTAL_FORMS": str(len(events)), "review_events-INITIAL_FORMS": str(len(events)),
                    "review_events-MIN_NUM_FORMS": "0", "review_events-MAX_NUM_FORMS": "1000", action: "1", **fields}
            for index, event in enumerate(events):
                data[f"review_events-{index}-id"] = str(event.pk)
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
            candidate.refresh_from_db()

        with patch("store.image_finder.review.download_image") as download:
            post("_approve_match", **{field: "on" for field in MATCH_CONFIRMATIONS})
            self.assertEqual(candidate.match_status, "approved")
            self.assertEqual(candidate.rights_status, "unknown")
            post("_verify_rights", rights_holder="Supplier", license_name="Written permission",
                 rights_evidence="Written agreement permits downloading and commercial publication of this photograph.",
                 commercial_reuse_confirmed="on", download_permission_confirmed="on")
            self.assertEqual(candidate.rights_status, "verified")
            download.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory), patch("store.image_finder.review.download_image", return_value=(photo_bytes(), "image/png", candidate.image_url)) as download:
            post("_publish_image", rights_holder="Supplier", license_name="Written permission",
                 rights_evidence="Written agreement permits downloading and commercial publication of this photograph.",
                 commercial_reuse_confirmed="on", download_permission_confirmed="on")
            self.assertIsNotNone(candidate.published_at)
            download.assert_called_once()


class ImageFileAndNetworkTests(SimpleTestCase):
    def test_rejects_tiny_wrong_format_and_oversized_files(self):
        for data, content_type in ((photo_bytes((10, 10)), "image/png"), (b"<svg/>", "image/png"), (photo_bytes(), "image/jpeg")):
            with self.assertRaises(ImageDownloadError):
                optimize_image(data, content_type)
        with override_settings(PRODUCT_IMAGE_MAX_BYTES=20):
            with self.assertRaises(ImageDownloadError):
                optimize_image(photo_bytes(), "image/png")

    def test_preserves_aspect_ratio_and_limits_output_dimensions(self):
        output, info = optimize_image(photo_bytes((1200, 2400)), "image/png")
        self.assertEqual((info["width"], info["height"]), (800, 1600))
        self.assertEqual(Image.open(io.BytesIO(output)).format, "WEBP")

    def test_unsafe_urls_and_mixed_private_dns_are_rejected(self):
        for url in ("http://example.com/a.png", "https://localhost/a", "https://127.0.0.1/a", "https://169.254.169.254/a", "https://user:secret@example.com/a", "https://example.com:444/a"):
            self.assertFalse(public_https_url(url))
        with patch("store.image_finder.network.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaisesMessage(ImageDownloadError, "non-public"):
                resolve_public_host("https://public.example/photo.png")

    def test_https_connects_to_validated_ip_with_original_tls_hostname(self):
        response = Mock(status=200, headers={"Content-Type": "image/png"})
        response.stream.return_value = [b"image bytes"]
        pool = Mock()
        pool.request.return_value = response
        with patch("store.image_finder.network.resolve_public_host", return_value=("supplier.example", "8.8.8.8")), patch("store.image_finder.network.urllib3.HTTPSConnectionPool", return_value=pool) as factory:
            pinned_get("https://supplier.example/photo.png", 100, Mock())
        self.assertEqual(factory.call_args.args[0], "8.8.8.8")
        self.assertEqual(factory.call_args.kwargs["server_hostname"], "supplier.example")
        self.assertEqual(factory.call_args.kwargs["assert_hostname"], "supplier.example")

    def test_robots_denial_blocks_image_transfer(self):
        with patch("store.image_finder.network.resolve_public_host", return_value=("supplier.example", "8.8.8.8")), patch("store.image_finder.network.bounded_get", return_value=(200, {}, b"User-agent: *\nDisallow: /", "https://supplier.example/robots.txt")), patch("store.image_finder.network.pinned_get") as get:
            with self.assertRaisesMessage(ImageDownloadError, "robots.txt"):
                download_image("https://supplier.example/image.png")
        get.assert_not_called()

    def test_redirect_destination_gets_robots_checked_before_image_request(self):
        with patch("store.image_finder.network.resolve_public_host", return_value=("supplier.example", "8.8.8.8")), patch("store.image_finder.network.check_robots") as robots, patch("store.image_finder.network.pinned_get", side_effect=[(302, {"Location": "https://cdn.example/image.png"}, b""), (200, {"Content-Type": "image/png"}, b"bytes")]):
            _, _, final = download_image("https://supplier.example/image.png")
        self.assertEqual(final, "https://cdn.example/image.png")
        self.assertEqual([call.args[0] for call in robots.call_args_list], ["https://supplier.example/image.png", "https://cdn.example/image.png"])

    def test_provider_thumbnail_is_not_original_and_rights_never_derived(self):
        result = normalize_result({"title": "Acme Pink Gin", "source_url": "https://supplier.example/acme", "image_url": "https://supplier.example/photo.png", "width": 600, "height": 1200, "thumbnail_url": "https://supplier.example/preview.png", "preview_permitted": True}, "Supplier feed agreement")
        self.assertTrue(result["preview_permitted"])
        self.assertNotEqual(result["thumbnail_url"], result["image_url"])
        self.assertEqual(result["metadata"]["rights_claim"], "Unverified")

    def test_original_cannot_be_embedded_as_a_review_thumbnail(self):
        result = normalize_result({"title": "Acme", "source_url": "https://supplier.example/acme", "image_url": "https://supplier.example/photo.png", "thumbnail_url": "https://supplier.example/photo.png", "preview_permitted": True}, "Supplier feed permission")
        self.assertFalse(result["preview_permitted"])
        self.assertEqual(result["thumbnail_url"], "")

    def test_long_retry_after_is_not_ignored(self):
        with self.assertRaisesMessage(DiscoveryError, "retry later"):
            retry_delay({"Retry-After": "120"}, 0)

    def test_feed_requires_documented_discovery_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.json"
            path.write_text('{"items": []}')
            with self.assertRaises(DiscoveryError):
                FeedProvider(path)

