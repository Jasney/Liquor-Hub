import hashlib
import ipaddress
import json
import re
from decimal import Decimal
from urllib.parse import urlsplit


def public_https_url(value):
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443}:
            return False
        if not host or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
            return False
        if host.lower().endswith((".localhost", ".local", ".internal")) or host.lower() == "localhost":
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", host.lower()))
    except (ValueError, TypeError):
        return False


def tokens(value):
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def size_values(value):
    result = set()
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(mls?|litres?|l|kgs?|g|oz)\b", value, re.I):
        unit = unit.lower()
        number = Decimal(number)
        if unit.startswith("ml"):
            result.add(("ml", number))
        elif unit in {"l", "litre", "litres"}:
            result.add(("ml", number * 1000))
        elif unit.startswith("kg"):
            result.add(("g", number * 1000))
        else:
            result.add((unit, number))
    return result


def packaging(value):
    value = value.casefold()
    result = set(re.findall(r"\b(?:can|cans|kit|kits|pod|pods|gift|set|case|pack|bottle|bottles|tetra|bag|pouches)\b", value))
    result = {re.sub(r"s$", "", token) for token in result}
    result.update(re.sub(r"\s+", "", match) for match in re.findall(r"\d+\s*(?:packs?|['’]s|pouches|puffs?|dots?)\b", value))
    return result


def snapshot(product):
    data = {"name": product.name, "category": product.category.name,
            "variants": sorted((v.pk, v.bottle_size, v.unit) for v in product.variants.all())}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def relevant_sources(product, sources):
    identity = product.name.casefold()
    relevant = [source for source in sources if source.is_active and source.verification_evidence.strip()
            and (source.kind != "manufacturer" or bool(source.brand_keywords.strip()))
            and (not source.brand_keywords or any(word.strip().casefold() in identity
                 for word in source.brand_keywords.split(",") if word.strip()))]
    return sorted(relevant, key=lambda source: ({"manufacturer": 0, "supplier": 1, "retailer": 2}.get(source.kind, 3), source.priority, source.pk or 0))


def build_queries(product, sources=(), maximum=6):
    sizes = list(dict.fromkeys(v.bottle_size for v in product.variants.all())) or [""]
    sizes = [size if size_values(size) else "" for size in sizes]
    sources = relevant_sources(product, sources)
    queries = []
    for size in sizes:
        base = f'"{product.name}" {size} {product.category.name} product photograph'.strip()
        preferred = sources[:2]
        if preferred:
            sites = " OR ".join(f"site:{s.domain}" for s in preferred)
            queries.append(f"{base} ({sites})")
        queries.append(base)
    # Bound query length for source adapters: 400 characters / 50 words.
    return list(dict.fromkeys(" ".join(q.split()[:50])[:400] for q in queries))[:maximum]


def score_candidate(product, result, sources=()):
    """Rank title/structured supplier evidence, NEVER the image filename.

    This is metadata ranking, not visual authentication. No score changes an
    approval state. Missing/contradictory evidence is explicit in match_checks.
    """
    attributes = result.get("attributes") or {}
    evidence = " ".join([result.get("title", ""), *[str(v) for v in attributes.values()]])
    expected = tokens(product.name)
    actual = tokens(evidence)
    coverage = len(expected & actual) / max(1, len(expected))
    missing = sorted(expected - actual)
    # All name tokens count, so flavours, ages, colours, editions and strengths
    # remain identity evidence. URLs/filenames contribute zero points.
    brand_token = next(iter(re.findall(r"[a-z0-9]+", product.name.casefold())), "")
    brand_present = bool(brand_token and brand_token in actual)
    observed_sizes = size_values(evidence)
    variants = list(product.variants.all())
    matched = [v for v in variants if size_values(v.bottle_size) & observed_sizes]
    variant = matched[0] if len(matched) == 1 else None
    expected_sizes = set().union(*(size_values(v.bottle_size) for v in variants)) if variants else set()
    size_state = "matched" if matched else "conflict" if observed_sizes and expected_sizes else "unknown"
    expected_pack = packaging(product.name)
    actual_pack = packaging(evidence)
    # Unlabelled packaging is unknown, never assumed to mean a single bottle.
    unexpected_pack = actual_pack - expected_pack - {"bottle"}
    pack_state = "conflict" if unexpected_pack or expected_pack - actual_pack else "matched" if expected_pack else "unspecified"
    host = (urlsplit(result.get("source_url", "")).hostname or "").lower()
    source = next((s for s in relevant_sources(product, sources)
                   if host == s.domain or host.endswith("." + s.domain)), None)
    width, height = result.get("width", 0), result.get("height", 0)
    quality = min(width, height) >= 300 and max(width, height) >= 600
    score = round(coverage * 60) + (15 if size_state == "matched" else 0)
    score += 5 if pack_state == "matched" else 0
    score += ({"manufacturer": 10, "supplier": 8, "retailer": 5}.get(source.kind, 0) if source else 0)
    score += 10 if quality else 0
    if not brand_present:
        score = min(score, 25)
    if missing:
        score = min(score, 60)
    if size_state == "conflict" or pack_state == "conflict":
        score = min(score, 35)
    checks = {
        "evidence_basis": "API title / structured supplier metadata; not filename or visual authentication",
        "brand_token_present": brand_present, "name_coverage": round(coverage, 3),
        "missing_identity_tokens": missing, "size": size_state,
        "packaging": pack_state, "expected_packaging": sorted(expected_pack),
        "observed_packaging": sorted(actual_pack), "source_kind": source.kind if source else "unverified",
        "dimensions_sufficient_claimed": quality, "dimensions_are_verified": False,
        "human_visual_review_required": True,
    }
    return min(100, score), checks, variant, source
