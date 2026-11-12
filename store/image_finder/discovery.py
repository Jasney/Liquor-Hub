import hashlib

from django.db import transaction

from store.models import ImageCandidate

from .providers import CandidateProvider

from .matching import build_queries, relevant_sources, score_candidate, snapshot


def discover_product(product, provider: CandidateProvider, sources, run=None, max_queries=6):
    existing = list(product.image_candidates.all())
    capacity = max(0, 5 - len(existing))
    if not capacity:
        return {"queries": 0, "found": 0, "created": 0, "already_present": len(existing)}
    queries = build_queries(product, sources, maximum=max_queries)
    results = {}
    queries_executed = 0
    for query in queries:
        queries_executed += 1
        for result in provider.search(product, query):
            # Supplier thumbnail embedding also requires a configured verified
            # source domain. Original images are never used as review previews.
            if result["preview_permitted"]:
                from urllib.parse import urlsplit
                host = (urlsplit(result["thumbnail_url"]).hostname or "").lower()
                result["preview_permitted"] = any(
                    host == s.domain or host.endswith("." + s.domain) for s in relevant_sources(product, sources)
                )
            result["query"] = query
            score, checks, variant, source = score_candidate(product, result, sources)
            result.update(confidence=score, match_checks=checks, variant=variant, source=source)
            if result["image_url"] not in results or results[result["image_url"]]["confidence"] < score:
                results[result["image_url"]] = result
        # Stop spending once enough exact identity/size candidates meet the
        # metadata quality threshold. These still require both human reviews.
        strong = [r for r in results.values() if r["confidence"] >= 75
                  and not r["match_checks"]["missing_identity_tokens"]
                  and r["match_checks"]["size"] == "matched"
                  and r["match_checks"]["packaging"] != "conflict"]
        if len(strong) >= capacity:
            break
    known = {candidate.image_url_hash for candidate in existing}
    ranked = sorted(results.values(), key=lambda result: (-result["confidence"], result["image_url"]))
    created = 0
    fingerprint = snapshot(product)
    with transaction.atomic():
        # Serialize candidate insertion per product to enforce the five-result
        # cap even if discovery jobs overlap; the unique hash also deduplicates.
        type(product).objects.select_for_update().get(pk=product.pk)
        capacity = max(0, 5 - ImageCandidate.objects.filter(product=product).count())
        for result in ranked:
            image_hash = hashlib.sha256(result["image_url"].encode()).hexdigest()
            if image_hash in known or created >= capacity:
                continue
            _, is_new = ImageCandidate.objects.get_or_create(
                product=product, image_url_hash=image_hash,
                defaults={
                    "variant": result["variant"], "source": result["source"], "search_run": run,
                    "provider": provider.name, "query": result["query"], "title": result["title"],
                    "source_url": result["source_url"], "image_url": result["image_url"],
                    "thumbnail_url": result["thumbnail_url"], "preview_permitted": result["preview_permitted"],
                    "width": result["width"], "height": result["height"], "confidence": result["confidence"],
                    "match_checks": result["match_checks"], "product_snapshot": fingerprint,
                    "metadata": result["metadata"],
                },
            )
            known.add(image_hash)
            created += int(is_new)
    return {"queries": queries_executed, "found": len(results), "created": created, "already_present": len(existing)}
