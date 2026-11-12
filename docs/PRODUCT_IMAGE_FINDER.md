# Product image discovery and review

The finder reads Django products and variants. It does not import catalogue CSVs, seed products, search during customer requests, or automatically grant image permissions. The catalogue import remains a separate approval step.

## Setup

The additive `store.0006` migration creates image-source, candidate, search-run and review-event records, plus an optional variant image field. It is applied locally so the updated website/admin can run. No catalogue records, sources or example candidates are seeded.

Discovery is source independent. No search API credentials are needed. Private `.env`
settings remain excluded from Git. Generic download controls are
`PRODUCT_IMAGE_REQUEST_INTERVAL` (default 1.1 seconds) and
`PRODUCT_IMAGE_RETRIES` (default 2).

Drinksvine has a separate, operator-invoked extraction command. The owner has
stated that bulk downloading and commercial reuse are permitted; record the
permission evidence during Admin rights review. The command crawls a bounded set
of source pages and stages permitted images for matching and human review. It is
not run automatically, and it never approves or publishes images.

Under **Admin → Image sources**, add verified manufacturers, authorized suppliers and reputable retailers. Record a hostname, source type, verification evidence and priority. Manufacturers need relevant brand keywords; supplier/retailer keywords are optional. Relevant manufacturers are preferred over suppliers and retailers. Domains are not declared official/authorized automatically, and configured source reputation never establishes commercial image rights.

## Discovery commands

### Plan a batch

```bash
python manage.py find_product_images --dry-run --limit 10 --missing-only --catalogue-only
```

Dry-run only plans queries: no provider calls or database writes. A real run
requires an explicit `--feed`; there is no default online provider. Category filters,
`--product-ids`, `--include-existing`, `--max-queries-per-product` (1–6), and JSON
`--report` remain available. Missing-only is the default. Inactive records and
products with existing product or variant images are skipped.

Queries preserve the product name, flavour, edition, packaging, category and explicit
variant sizes. Filename similarity never establishes a match. Discovery retains up
to five unique candidates per product, including existing rejected/skipped records.
Repeats preserve decisions and deduplicate URLs. Customer requests never search.

New source-specific adapters implement `CandidateProvider.search(product, query)`
in `store/image_finder/providers.py`, return `normalize_result` metadata, and are
passed to `discover_product`. That engine performs matching, verified-domain preview
checks, deduplication and candidate storage without a vendor-specific branch.
Adapters must respect source permissions, request limits and bounded retries;
the generic `RequestGate` and `retry_delay` helpers are also used by the safe downloader.
The generic command currently wires only the local feed adapter. Source
configuration errors occur before creating a run; per-product failures are
reported without publishing. Drinksvine extraction is handled by the separate
command below.

### Drinksvine extraction

Run the bounded Drinksvine crawl explicitly when you have confirmed the recorded
permission evidence:

```bash
python manage.py extract_drinksvine_images --limit 20
```

This command stages downloaded assets and writes match/review reports under
`catalogue_data/drinksvine_pilot` by default. It does not approve or publish
images. Page, request, and product limits can be adjusted with `--max-pages`,
`--max-requests`, `--listing-pages`, and `--limit`; batches above 20 require
`--approved-batch` and support up to 100 products.

To recover source product metadata and direct image URL leads from previously
validated local cache entries without network access or database writes, run:

```bash
python manage.py extract_drinksvine_images --cache-only
```

This writes `drinksvine_cached_image_leads.csv` and a JSON report under
`catalogue_data/drinksvine_pilot`. Each row retains its product page and source
image URL; rights remain unconfirmed and no image bytes are downloaded.

### Permitted supplier feeds

Use a locally supplied, permitted JSON feed:

```bash
python manage.py find_product_images --feed /path/to/permitted-supplier-feed.json --limit 20 --report image-discovery.json
```

Schema example (replace all placeholder values using a supplier's documented feed permission):

```json
{
  "permission_reference": "Supplier agreement/reference permitting feed access and metadata storage",
  "items": [
    {
      "title": "Exact catalogue product name 750ml",
      "source_url": "https://supplier.example/product",
      "image_url": "https://supplier.example/media/product.png",
      "thumbnail_url": "https://supplier.example/media/review-thumbnail.png",
      "preview_permitted": false,
      "width": 600,
      "height": 1200,
      "attributes": {
        "brand": "Exact brand",
        "name": "Exact product name",
        "flavour": "Exact flavour if applicable",
        "edition": "Exact edition if applicable",
        "size": "750ml",
        "packaging": "single bottle"
      },
      "rights_claim": "Supplier's unverified licence/usage claim"
    }
  ]
}
```

Feed access permission is separate from image downloading/commercial reuse permission. Feed previews additionally require explicit preview permission and a thumbnail host under a configured, verified source domain. If no permitted thumbnail exists, the admin shows the source-page link for manual inspection instead. No original image is downloaded for review. Do not use an original image URL as the thumbnail URL.

## Ranking and limitations

Confidence is a transparent metadata heuristic, not a calibrated probability or visual authenticity certification:

- Up to 60 points: product-name token coverage, including brand, flavour and edition.
- Up to 15: explicit size matching a database variant (1L and 1000ml compare as the same capacity).
- Up to 5: explicit matching packaging.
- Up to 10: configured source verification/reputation.
- Up to 10: claimed resolution sufficient for a product image.

Missing identity tokens, conflicting sizes or conflicting packaging cap scores. Image filenames and image URL paths contribute no match points. Supplier/API dimensions and claims are unverified until download validation; all candidates need human visual review regardless of score. Missing flavour/edition data is never fabricated from the catalogue name. Multiple-size products can have candidates targeting specific variants; a reviewer can select the correct target variant or main product photo before approving.

## Admin workflow

Visit `/admin/store/imagecandidate/` (**Image review**). Each candidate shows its product/sizes, target variant, permitted thumbnail, source website/page, original image URL, confidence explanation, provider/supplier claims and both review states.

1. Check the source/preview against the actual product: brand, flavour/edition, size, packaging, clarity and authenticity. Tick all five visual checks, select the correct target, and choose **Approve product match**, **Reject**, or **Skip**. This neither grants rights nor downloads the original.
2. Independently record the rights holder, licence/agreement, scope and permission evidence. Confirm that commercial publication and downloading are both permitted. Record any required attribution. Choose **Verify commercial reuse rights**. Source reputation, search visibility and a provider's rights claim are not sufficient on their own.
3. Choose **Download and publish verified image**. Missing approvals, stale product details, changed URLs/evidence, or existing target images block the operation before original transfer. The approvals are checked again before storage to guard against a concurrent manual upload/review change.
4. A published record is locked for review edits. **Withdraw published image** removes its managed target reference and schedules file deletion after transaction commit, while retaining provenance/review history. A manually replaced image is preserved. Re-publication requires renewed reviews.

No bulk action auto-approves matches or rights. Rights verification records the administrator's checked evidence; software does not independently determine whether an agreement grants legal rights. Admin controls intentionally separate this decision from visual matching.

Grant staff `view_imagecandidate` and `change_imagecandidate` to access review forms. Separately grant only the duties they need:

- `review_image_matches`: Approve, Reject, Skip.
- `verify_image_rights`: Edit/verify commercial permission evidence.
- `publish_product_images`: Publish and Withdraw.
- ImageSource view/add/change permissions to manage preferred sources.
- ImageSearchRun view permission to read job reports.

Superusers have all duties. No real staff accounts or permissions were modified during implementation. Normal product/variant admin image uploads remain available and are never overwritten by this pipeline.

If a product name/category/size changes after discovery, refresh metadata explicitly:

```bash
python manage.py refresh_product_image_checks CANDIDATE_ID
```

This resets the match decision and requires a new visual review. Withdraw a published image before refreshing it. It does not download an original or verify rights.

## Original download and storage

Only the explicit Publish operation downloads the original. The download client uses public HTTPS endpoints, validates DNS addresses, pins the connection to the validated public IP with the original TLS hostname/certificate checks, revalidates redirect destinations, respects robots rules/crawl delays, and uses no source authentication or bypass techniques. Restrictions or robots/network failures stop the transfer. [urllib3 documents the custom-SNI/TLS hostname mechanism](https://urllib3.readthedocs.io/en/stable/advanced-usage.html#custom-sni-hostname).

Limits are 8 MB downloaded, at least 300px on each side / 600px on the longer side, at most 25 megapixels, JPEG/PNG/static WebP only, and a bounded number of redirects/retries. Signature checks reject mislabeled formats, SVG, corrupt data, animation and decompression bombs. [Pillow documents its image security limits](https://pillow.readthedocs.io/en/stable/handbook/security.html). Images are resized within 1600×1600 preserving aspect ratio and transparency, stripped of embedded metadata, and saved as WebP through Django media storage. No crop, fake bottle rendering or branded image generation is performed.

Descriptive filenames include product, target size, candidate ID and a content hash. The database retains original/final URLs, source information, licence/evidence, attribution, reviewers, timestamps, actual dimensions, byte counts and checksums. Audit events survive rejection, skip and withdrawal. Product/variant image fields reference the stored file; existing manual images are preserved. Product cards, size selection and carts use the relevant image or the local placeholder, with published-image attribution displayed alongside it. A variant without a photo uses a manually supplied main product photo or the placeholder, never another size's photo.

Media storage should be persistent in production, as already required by this project. Configure public media delivery according to the verified licence scope; the local development server is not a production deployment.

## Reports and verification

The finder prints JSON and optionally writes `--report`. Reports contain selected/processed products, planned/executed queries, distinct candidates found/created, products still without candidates, separate pending/match-approved/rights-verified/published/rejected/skipped counts, and per-product errors. Search-run records are visible under **Image search reports**. Discovery always reports zero originals downloaded/published; publication has its own timestamp/event/download audit.

Tests use disposable databases, generated unbranded test pixels and mocked provider/download responses. They cover query generation, metadata scoring, filename-only false positives, candidate deduplication/caps, dry-run isolation, permitted feeds, retry limits, separate approvals, stale review detection, permissions, manual-image preservation, actual admin buttons, safe URLs/DNS/TLS, robots/redirects, corrupt/small/oversized formats, aspect ratio, attribution, variant association and withdrawal. No authentic product photographs are represented by the test pixels or seeded into the shop.
