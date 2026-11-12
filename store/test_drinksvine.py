import csv
import hashlib
import io
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib import admin
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .image_finder.drinksvine import (DrinksVineProvider, SourceClient, discover_urls,
    extract_cached_product_pages, extract_product, identity, permitted_image, select_pages, source_url, stage_image)
from .image_finder.discovery import discover_product
from .image_finder.network import ImageDownloadError
from .image_finder.providers import DiscoveryError
from .image_finder.review import MATCH_CONFIRMATIONS, ReviewError, optimize_image, publish_candidate, review_match
from .models import ImageCandidate, ImageSearchRun, Product, ProductVariant, StagedSourceImage
from .management.commands.extract_drinksvine_images import select_products
from .test_product_images import ProductImageData, photo_bytes


def html(name='Acme Pink Gin', sizes=('750 ml',), image='https://ik.imagekit.io/drinksvine/products/acme.webp'):
    data = {'@graph': [{'@type': 'ProductGroup', 'name': name, 'brand': {'name': 'Acme'}}] + [
        {'@type': 'Product', 'name': name + ' ' + size, 'size': size, 'image': [{'url': image}]} for size in sizes]}
    return '<script type="application/ld+json">' + json.dumps(data) + '</script><img alt="Other Gin" src="https://ik.imagekit.io/drinksvine/products/other.webp">'


class ExtractionTests(SimpleTestCase):
    def test_cache_only_extraction_reuses_hashed_html_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            url = "https://drinksvine.co.ke/product/acme"
            key = hashlib.sha256(url.encode()).hexdigest()
            body = html().encode()
            (cache / f"{key}.bin").write_bytes(body)
            (cache / f"{key}.json").write_text(json.dumps({"content_type": "text/html", "sha256": hashlib.sha256(body).hexdigest(), "final_url": url}))
            result = extract_cached_product_pages(cache)
        self.assertEqual(result["html_pages_examined"], 1)
        self.assertEqual(len(result["products"]), 1)
        self.assertEqual(result["products"][0]["images"][0]["url"], "https://ik.imagekit.io/drinksvine/products/acme.webp")
        self.assertFalse(result["errors"])

    def test_cache_only_extraction_rejects_modified_cached_body(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            url = "https://drinksvine.co.ke/product/acme"
            key = hashlib.sha256(url.encode()).hexdigest()
            (cache / f"{key}.bin").write_bytes(b"changed")
            (cache / f"{key}.json").write_text(json.dumps({"content_type": "text/html", "sha256": "wrong", "final_url": url}))
            result = extract_cached_product_pages(cache)
        self.assertEqual(result["products"], [])
        self.assertIn("SHA-256", result["errors"][0]["error"])


    def test_structured_sizes_and_shared_images_not_inferred_from_filename(self):
        product = extract_product(html(sizes=('750 ml', '1 litre')), 'https://drinksvine.co.ke/product/acme')
        self.assertEqual(product['sizes'], ['750 ml', '1 litre'])
        self.assertEqual(len(product['images']), 1)
        self.assertEqual(product['images'][0]['sizes'], ['750 ml', '1 litre'])
        self.assertEqual(product['name'], 'Acme Pink Gin')

    def test_missing_size_stays_missing_and_related_products_are_excluded(self):
        product = extract_product(html(sizes=('',)), 'https://drinksvine.co.ke/product/acme')
        self.assertEqual(product['sizes'], [])
        self.assertEqual(len(product['images']), 1)

    def test_unrelated_source_links_and_cdn_accounts_rejected(self):
        self.assertIsNone(source_url('https://example.org/product/acme'))
        self.assertFalse(permitted_image('https://ik.imagekit.io/another/products/a.webp'))
        self.assertFalse(permitted_image('https://user:pass@drinksvine.co.ke/products/a.webp'))
        self.assertEqual(identity('Gordon’s Pink Gin'), identity('Gordons-Pink Gin'))

    def test_sitemap_categories_and_pagination_are_bounded_and_deduplicated(self):
        client = Mock()
        def get(url):
            if url.endswith('sitemap.xml'):
                return b'<urlset><url><loc>https://drinksvine.co.ke/product/one</loc></url></urlset>', 'application/xml', url
            if url.endswith('/gin?page=2'):
                return b'<a href="/product/two">two</a>', 'text/html', url
            if url.endswith('/gin'):
                return b'<a href="/gin?page=2">next</a>', 'text/html', url
            return b'<a href="/gin">Gin</a><a href="/product/one">One</a>', 'text/html', url
        client.get.side_effect = get
        urls, errors, _ = discover_urls(client, 3)
        self.assertEqual(len(urls), 2)
        self.assertFalse(errors)
        self.assertTrue(any(c.args[0].endswith('/gin?page=2') for c in client.get.call_args_list))

    def test_robots_denial_and_cross_domain_redirect_block_fetches(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SourceClient(directory)
            with patch('store.image_finder.drinksvine.pinned_get', return_value=(200, {}, b'User-agent: *\nDisallow: /')) as get:
                with self.assertRaises(DiscoveryError):
                    client.get('https://drinksvine.co.ke/product/acme')
                get.assert_called_once()
            client = SourceClient(directory)
            with patch('store.image_finder.drinksvine.pinned_get', side_effect=[(404, {}, b''), (302, {'Location': 'https://example.org/private'}, b'')]) as get:
                with self.assertRaises(DiscoveryError):
                    client.get('https://drinksvine.co.ke/product/acme')
                self.assertEqual(get.call_count, 2)

    def test_cache_and_request_cap_prevent_unnecessary_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SourceClient(directory, max_requests=2)
            with patch('store.image_finder.drinksvine.pinned_get', side_effect=[(404, {}, b''), (200, {'Content-Type': 'text/html'}, b'page')]) as get:
                client.get('https://drinksvine.co.ke/product/acme')
                self.assertEqual(client.get('https://drinksvine.co.ke/product/acme')[0], b'page')
                self.assertEqual(get.call_count, 2)
                with self.assertRaises(DiscoveryError):
                    client.get('https://drinksvine.co.ke/product/second')

    def test_transient_connection_failure_retries_with_bounded_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SourceClient(directory, max_requests=4)
            with patch('store.image_finder.drinksvine.pinned_get', side_effect=[
                    ImageDownloadError('temporary connect failure', retryable=True),
                    (404, {}, b''), (200, {'Content-Type': 'text/html'}, b'page')]) as get, \
                    patch('store.image_finder.drinksvine.time.sleep') as sleep:
                self.assertEqual(client.get('https://drinksvine.co.ke/product/acme')[0], b'page')
                self.assertEqual(get.call_count, 3)
                self.assertEqual(client.requests, 3)
                sleep.assert_called_once_with(1)

    def test_local_socket_permission_failure_fails_fast_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SourceClient(directory)
            with patch('store.image_finder.drinksvine.pinned_get', side_effect=ImageDownloadError(
                    'HTTPS connection blocked by local socket permissions.')) as get:
                with self.assertRaisesRegex(DiscoveryError, 'local socket permissions') as raised:
                    client.get('https://drinksvine.co.ke/product/acme')
                self.assertTrue(raised.exception.fatal)
                get.assert_called_once()


class CacheOnlyIntegrationTests(TestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

    def test_cache_only_command_reconciles_existing_asset_without_db_writes(self):
        from .models import StagedSourceImage
        from .test_product_images import photo_bytes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            output = root / "output"
            cache.mkdir()
            url = "https://drinksvine.co.ke/product/acme"
            key = hashlib.sha256(url.encode()).hexdigest()
            body = html(image="https://ik.imagekit.io/drinksvine/products/acme.webp").encode()
            (cache / f"{key}.bin").write_bytes(body)
            (cache / f"{key}.json").write_text(json.dumps({"content_type": "text/html", "sha256": hashlib.sha256(body).hexdigest(), "final_url": url}))
            asset = StagedSourceImage.objects.create(source_name="Drinks Vine", source_product_name="Acme Pink Gin",
                source_url=url, image_url="https://ik.imagekit.io/drinksvine/products/acme.webp", available_sizes=["750 ml"],
                image="image-staging/acme.png", sha256=hashlib.sha256(photo_bytes()).hexdigest(), width=300, height=300)
            asset.image.save("acme.png", ContentFile(photo_bytes()), save=True)
            call_command("extract_drinksvine_images", "--cache-only", "--cache", str(cache), "--output", str(output), verbosity=0)
            with (output / "drinksvine_cached_image_leads.csv").open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            report = json.loads((output / "drinksvine_cache_only_report.json").read_text())
        self.assertEqual(row["staged_record_id"], str(asset.pk))
        self.assertEqual(row["staged_image_valid"], "True")
        self.assertEqual(row["staged_hash_matches_record"], "True")
        self.assertEqual(report["matching_staged_assets"], 1)
        self.assertEqual(report["locally_valid_staged_assets"], 1)
        self.assertEqual(report["database_writes"], 0)
        self.assertEqual(ImageCandidate.objects.count(), 0)

class StagingTests(ProductImageData):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.directory.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def stage(self, sizes=('750 ml',)):
        client = Mock()
        client.get.return_value = (photo_bytes(), 'image/png', 'https://ik.imagekit.io/drinksvine/products/acme.webp')
        source = extract_product(html(sizes=sizes), 'https://drinksvine.co.ke/product/acme')
        asset, _ = stage_image(client, source, source['images'][0])
        return client, source, asset

    def propose(self, asset):
        discover_product(self.product, DrinksVineProvider([asset]), [])
        candidate = ImageCandidate.objects.get()
        candidate.staged_image = asset
        candidate.save(update_fields=['staged_image'])
        return candidate

    def test_staging_is_local_deduplicated_and_does_not_change_catalogue(self):
        client, source, asset = self.stage()
        stage_image(client, source, source['images'][0])
        client.get.assert_called_once()
        self.assertTrue(asset.image.storage.exists(asset.image.name))
        self.product.refresh_from_db()
        self.assertFalse(self.product.image)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, 2)
        self.assertEqual(str(self.variant.price), '1500.00')
        self.assertEqual(asset.queue, 'unmatched')

    def test_existing_staged_url_is_reused_without_downloader(self):
        client, source, asset = self.stage()
        client.get.reset_mock()
        reused, downloaded = stage_image(client, source, source['images'][0])
        self.assertEqual(reused.pk, asset.pk)
        self.assertFalse(downloaded)
        client.get.assert_not_called()

    def test_missing_staged_file_is_restored_on_same_row(self):
        client, source, asset = self.stage()
        old_name = asset.image.name
        asset.image.storage.delete(old_name)
        client.get.return_value = (photo_bytes(), 'image/png', 'https://ik.imagekit.io/drinksvine/products/acme.webp')
        restored, downloaded = stage_image(client, source, source['images'][0])
        self.assertEqual(restored.pk, asset.pk)
        self.assertTrue(downloaded)
        self.assertTrue(restored.image.storage.exists(restored.image.name))
        self.assertEqual(StagedSourceImage.objects.filter(image_url=source['images'][0]['url']).count(), 1)
        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(client.get.call_args, ((source['images'][0]['url'],), {'image': True}))

    def test_corrupt_cache_metadata_refetches_instead_of_aborting(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SourceClient(directory, max_requests=3)
            url = 'https://drinksvine.co.ke/product/acme'
            with patch('store.image_finder.drinksvine.pinned_get', side_effect=[
                    (404, {}, b''), (200, {'Content-Type': 'text/html'}, b'first'),
                    (200, {'Content-Type': 'text/html'}, b'fresh')]) as get:
                self.assertEqual(client.get(url)[0], b'first')
                cache = Path(directory) / (hashlib.sha256(url.encode()).hexdigest() + '.json')
                cache.write_text('{broken')
                self.assertEqual(client.get(url)[0], b'fresh')
                self.assertEqual(get.call_count, 3)

    def test_default_batch_skips_products_with_candidates_and_prior_attempts(self):
        self.product.catalogue_key = 'TEST-001'
        self.product.save(update_fields=['catalogue_key'])
        previously_attempted = ImageSearchRun.objects.create(
            provider='drinksvine', statistics={'product_ids': [self.product.pk], 'source_product_urls_discovered': 5})
        failed_product = Product.objects.create(name='Acme Recovery Gin', category=self.category, catalogue_key='TEST-003')
        ProductVariant.objects.create(product=failed_product, bottle_size='750ml', unit='Piece', price=Decimal('1000'), stock_quantity=1)
        ImageSearchRun.objects.create(provider='drinksvine', statistics={
            'product_ids': [failed_product.pk], 'source_product_urls_discovered': 0}, errors=[{'error': 'network failure'}])
        candidate_product = Product.objects.create(name='Acme Orange Gin', category=self.category, catalogue_key='TEST-002')
        ProductVariant.objects.create(product=candidate_product, bottle_size='750ml', unit='Piece', price=Decimal('1000'), stock_quantity=1)
        ImageCandidate.objects.create(
            product=candidate_product, provider='drinksvine', query='q', title='Acme Orange Gin',
            source_url='https://supplier.example/product', image_url='https://cdn.supplier.example/a.png',
            image_url_hash=hashlib.sha256(b'https://cdn.supplier.example/a.png').hexdigest(),
            product_snapshot='snapshot')
        selected = select_products(20)
        selected_ids = {product.pk for product in selected}
        self.assertNotIn(self.product.pk, selected_ids)
        self.assertNotIn(candidate_product.pk, selected_ids)
        self.assertNotIn(self.product.pk, selected_ids)
        self.assertIn(failed_product.pk, {product.pk for product in select_products(100)})
        self.assertEqual(select_products(20, [self.product.pk])[0].pk, self.product.pk)

    def test_failed_global_discovery_is_not_reported_as_unmatched(self):
        self.product.catalogue_key = 'TEST-FAIL'
        self.product.save(update_fields=['catalogue_key'])
        with tempfile.TemporaryDirectory() as directory:
            with patch('store.management.commands.extract_drinksvine_images.discover_urls', return_value=([], [{'url': 'https://drinksvine.co.ke/sitemap.xml', 'error': 'offline'}], False)):
                with self.assertRaises(CommandError):
                    call_command('extract_drinksvine_images', limit=1, output=Path(directory) / 'out', cache=Path(directory) / 'cache', stdout=io.StringIO())
            run = ImageSearchRun.objects.latest('pk')
            self.assertEqual(run.statistics['unmatched_products'], 0)
            self.assertEqual(run.statistics['products_not_processed'], 1)

    def test_shared_size_is_not_assigned_and_requires_specific_variant_review(self):
        _, _, asset = self.stage(sizes=('750 ml', '1 litre'))
        candidate = self.propose(asset)
        self.assertIsNone(candidate.variant)
        with self.assertRaisesMessage(ReviewError, 'specific target variant'):
            self.approve(candidate)
        self.assertEqual(candidate.match_status, 'pending')
        self.assertEqual(candidate.rights_status, 'unknown')

    def test_admin_shows_local_staging_and_separate_unmatched_queue(self):
        _, _, asset = self.stage()
        candidate = self.propose(asset)
        preview = admin.site._registry[ImageCandidate].candidate_preview(candidate)
        self.assertIn(asset.image.url, preview)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('admin:store_imagecandidate_change', args=[candidate.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin:store_stagedsourceimage_changelist') + '?queue=unmatched').status_code, 200)

    def test_publication_needs_both_approvals_and_uses_local_bytes(self):
        _, _, asset = self.stage()
        candidate = self.propose(asset)
        with patch('store.image_finder.review.download_image') as download:
            with self.assertRaises(ReviewError):
                publish_candidate(candidate.pk, self.user)
            self.approve(candidate)
            with self.assertRaises(ReviewError):
                publish_candidate(candidate.pk, self.user)
            self.license(candidate)
            publish_candidate(candidate.pk, self.user)
            download.assert_not_called()
        self.variant.refresh_from_db()
        self.assertTrue(self.variant.image)

    def test_tampered_staging_and_manual_upload_block_publication(self):
        _, _, asset = self.stage()
        candidate = self.propose(asset)
        self.approve(candidate)
        self.license(candidate)
        with asset.image.open('wb') as stream:
            stream.write(b'tampered')
        with self.assertRaisesMessage(ReviewError, 'checksum'):
            publish_candidate(candidate.pk, self.user)
        self.variant.image = 'manual.png'
        self.variant.save()
        with self.assertRaisesMessage(ReviewError, 'already has an image'):
            publish_candidate(candidate.pk, self.user)
