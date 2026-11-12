"""Conservative, reproducible parsing of the supplied line-oriented PDF extract.

No Django imports or database access. Run with ``python -m store.catalogue``.
"""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path


SOURCE = Path("catalogue_data/Liquor_Hub_Diani_Catalogue_Extracted.txt")
FIELDS = (
    "row_id", "category", "product_name", "size", "unit", "price_ksh",
    "packaging", "variant_label", "product_key", "variant_key", "source_name",
    "source_page", "source_line_start", "source_line_end", "raw_size_column",
    "raw_price", "flags", "review_status", "duplicate_of",
)
PRICE = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?")
UNIT = re.compile(r"(?:1 )?(?:Piece|Litre)|Meters|(?:\d+(?:\.\d+)?\s*(?:mls?|L)) Piece", re.I)
SIZE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?\s*(?:mls?|litres?|kgs?|kg|g|oz|l))\b", re.I)
PACK = re.compile(r"\b\d+\s*(?:packs?|pouches|puffs?|puffss|k puffs|['’]s)\b", re.I)
BEVERAGE_EXCEPTIONS = {"Accessories", "Cigars", "Condoms", "Extras", "Vapes", "Ice"}


def normalized(value):
    return " ".join(value.casefold().split())


def key(prefix, *parts):
    return prefix + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def normalized_size(value):
    value = re.sub(r"\s+", "", value.casefold())
    return re.sub(r"mls$", "ml", re.sub(r"kgs$", "kg", value))


def parse_catalogue(path=SOURCE):
    source = Path(path).read_text(encoding="utf-8")
    index = source.split("\nCATEGORIES\n", 1)[1].split("\nPrices are in", 1)[0].splitlines()
    category_names = {name.upper(): name for name in index}
    categories, rows, errors, pending = [], [], [], []
    category = None
    page = 0
    in_table = False
    lines = source.splitlines()
    for number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        match = re.fullmatch(r"=+ PDF PAGE (\d+) =+", line)
        if match:
            page = int(match[1])
            in_table = False
            continue
        if page < 3 or not line:
            continue
        # Every supplied page repeats these six header lines before its tables.
        if line == str(page - 1) and not in_table:
            in_table = True
            continue
        if not in_table:
            continue
        if line == "CHEZA NA DELIVERY":
            break
        if line.upper() in category_names:
            if pending:
                errors.append({"page": page, "lines": pending, "reason": "Unclosed row before category"})
                pending = []
            category = category_names[line.upper()]
            if category not in categories:
                categories.append(category)
            continue
        if line in {"ITEM", "SIZE", "PRICE"}:
            continue
        pending.append((number, line))
        if len(pending) < 3 or not UNIT.fullmatch(pending[-2][1]) or not PRICE.fullmatch(line):
            continue
        name_parts = pending[:-2]
        source_name = " ".join(part for _, part in name_parts)
        unit = pending[-2][1]
        sizes = list(SIZE.finditer(source_name))
        size = sizes[-1][1] if sizes else ""
        name = source_name
        if len(sizes) == 1:
            name = (source_name[:sizes[0].start()] + source_name[sizes[0].end():]).strip()
            name = " ".join(name.split())
        flags = []
        if not category:
            flags.append("missing_category")
        if len(sizes) > 1:
            flags.append("multiple_sizes_in_name")
        if not size and category not in BEVERAGE_EXCEPTIONS:
            flags.append("missing_size")
        if normalized(unit) in {"litre", "1 litre"}:
            if normalized_size(size) not in {"1l", "1000ml", "1litre"}:
                flags.append("size_column_litre_conflict")
        if "piece" in unit.casefold() and normalized(unit) not in {"piece", "1 piece"}:
            unit_size = re.sub(r"\s*piece$", "", unit, flags=re.I)
            if normalized_size(unit_size) != normalized_size(size):
                flags.append("size_column_piece_conflict")
        if re.search(r"\bIL\b", source_name):
            flags.append("possible_ocr_IL_size")
        if category == "Cigars" and re.search(r"vape|puff|pouch|velo|nicotine|\bdot", source_name, re.I):
            flags.append("category_mismatch_possible")
        if category == "Extras" and re.search(r"\bvape|\bpod|\bpuffs|\bDelivery|courier", source_name, re.I):
            flags.append("nonstandard_extra_or_service")
        if category == "Accessories" and re.search(r"\bDelivery|\bIce Blocks|\bVelo|Miscellaneous", source_name, re.I):
            flags.append("nonstandard_accessory_or_service")
        if category == "Sparkling Wine" and re.search(r"\b0\.0%|Zero|Absolute Zero", source_name, re.I):
            flags.append("non_alcoholic_in_sparkling_wine")
        if re.search(r"\b(?:Delivery|courier|Miscellaneous Sales)\b", source_name, re.I):
            flags.append("service_listing_review")
        packaging = "; ".join(m[0] for m in PACK.finditer(source_name))
        # Packaging counts remain in the product name: packs, pods, kits and
        # individual bottles must never collapse into the same product.
        label = size or unit
        product_key = key("LHC-P-", normalized(category or ""), normalized(name))
        variant_key = key("LHC-V-", product_key, normalized_size(size), normalized(unit))
        rows.append(dict(zip(FIELDS, (
            f"LH2026-{len(rows) + 1:04d}", category, name, size, unit,
            line.replace(",", ""), packaging, label, product_key, variant_key,
            source_name, page, pending[0][0], number, unit, line,
            ";".join(flags), "pending" if flags else "ready", "",
        ))))
        pending = []
    if pending:
        errors.append({"page": page, "lines": pending, "reason": "Unclosed final row"})

    # Same name/size but different raw units are still potential duplicates.
    identities = defaultdict(list)
    for row in rows:
        identities[(row["product_key"], normalized_size(row["size"]), normalized(row["unit"]) if not row["size"] else "")].append(row)
    duplicates = []
    for group in identities.values():
        if len(group) < 2:
            continue
        duplicates.append([r["row_id"] for r in group])
        same_price = len({r["price_ksh"] for r in group}) == 1
        for row in group:
            row["flags"] = ";".join(filter(None, [row["flags"], "duplicate_entry" if same_price else "conflicting_duplicate_price"]))
            row["review_status"] = "pending"
        if same_price:
            for row in group[1:]:
                row["duplicate_of"] = group[0]["row_id"]

    # Similar names are review suggestions only; never use fuzzy matching to merge.
    near_matches = []
    buckets = defaultdict(list)
    for row in rows:
        buckets[(row["category"], normalized_size(row["size"]))].append(row)
    for group in buckets.values():
        for i, left in enumerate(group):
            for right in group[i + 1:]:
                a, b = normalized(left["product_name"]), normalized(right["product_name"])
                if a != b and SequenceMatcher(None, a, b).ratio() >= .90:
                    near_matches.append([left["row_id"], right["row_id"]])
                    for row in (left, right):
                        if "similar_name_review" not in row["flags"].split(";"):
                            row["flags"] = ";".join(filter(None, [row["flags"], "similar_name_review"]))
                            row["review_status"] = "pending"
    alias_groups = defaultdict(list)
    for row in rows:
        # Only propose review: even generic spirit words may distinguish items.
        alias = re.sub(r"\b(?:gin|whisky|whiskey|vodka|rum|cream|vermouth|cognac)\b", "", normalized(row["product_name"]))
        alias = re.sub(r"[^a-z0-9]", "", alias)
        alias_groups[(row["category"], alias)].append(row)
    aliases = []
    for group in alias_groups.values():
        if len({row["product_key"] for row in group}) <= 1:
            continue
        aliases.append([row["row_id"] for row in group])
        for row in group:
            row["flags"] = ";".join(filter(None, [row["flags"], "possible_name_alias"]))
            row["review_status"] = "pending"
    # Report aliases alongside other similar-name suggestions without merging.
    for group in aliases:
        for row_id in group[1:]:
            pair = [group[0], row_id]
            if pair not in near_matches:
                near_matches.append(pair)
    return source, index, categories, rows, errors, duplicates, near_matches


def export_catalogue(path=SOURCE, output_dir=None):
    path = Path(path)
    out = Path(output_dir or path.parent)
    out.mkdir(parents=True, exist_ok=True)
    source, index, categories, rows, errors, duplicates, near_matches = parse_catalogue(path)
    csv_path = out / "liquor_catalogue_2026.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    counts = Counter(r["category"] for r in rows)
    with (out / "categories_2026.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["category", "display_order", "entry_count"])
        writer.writerows((name, i, counts[name]) for i, name in enumerate(categories, 1))
    flags = Counter(flag for row in rows for flag in row["flags"].split(";") if flag)
    report = {
        "source": str(path), "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "csv_sha256": digest, "pdf_verified": False,
        "expected_categories_in_request": 42, "index_category_count": len(index),
        "category_count": len(categories), "missing_index_categories": sorted(set(index) - set(categories)),
        "entries": len(rows), "unique_products_proposed": len({r["product_key"] for r in rows}),
        "unique_variants_proposed": len(identities := {
            (r["product_key"], normalized_size(r["size"]), normalized(r["unit"]) if not r["size"] else "") for r in rows
        }),
        "ready_entries": sum(r["review_status"] == "ready" for r in rows),
        "review_entries": sum(r["review_status"] == "pending" for r in rows),
        "invalid_or_missing_prices": [r["row_id"] for r in rows if not PRICE.fullmatch(r["raw_price"]) or float(r["price_ksh"]) <= 0],
        "parse_errors": errors, "duplicate_groups": duplicates, "similar_name_pairs": near_matches,
        "flag_counts": dict(flags), "category_counts": dict(counts),
        "review_rows": [r for r in rows if r["review_status"] == "pending"],
    }
    (out / "validation_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    md = [
        "# Liquor Hub DIANI 2026 — catalogue validation", "",
        "Status: **AWAITING APPROVAL — NO DATABASE IMPORT PERFORMED.**", "",
        "## Findings", "",
        f"- Category index: **{len(index)}**; table categories: **{len(categories)}**. The request says 42; all source categories are preserved without renaming or merging.",
        f"- Source entries: **{len(rows)}**; conservative unique product groups: **{report['unique_products_proposed']}**; proposed unique variants: **{len(identities)}**.",
        f"- Ready entries: **{report['ready_entries']}**; entries requiring review: **{report['review_entries']}**.",
        f"- Duplicate groups: **{len(duplicates)}**; similar-name pairs: **{len(near_matches)}**.",
        f"- Missing/invalid extracted prices: **{len(report['invalid_or_missing_prices'])}**; unparsed fragments: **{len(errors)}**.",
        "- Stock quantities and actual product images are absent. New variants must start at zero stock; no descriptions or images will be fabricated.",
        "- The original PDF is unavailable. Column alignment and ambiguous text cannot be verified against the PDF. Counts reflect the supplied extract, not independently verified PDF completeness.", "",
        "## Reconstruction rules", "",
        "The extract serializes two repeated ITEM/SIZE/PRICE headers, then row cells. A row ends only when a recognized unit cell is followed by a numeric price. Wrapped name/size lines are joined; category context continues across page breaks. Every row includes PDF page and original text line numbers. Unclosed fragments are reported rather than discarded.", "",
        "`size` comes only from an explicit capacity or weight in the item name. `unit` and `raw_size_column` retain the literal table SIZE cell (Piece, 1 Piece, Litre, etc.). Litre is never silently converted into a bottle size. Blank size for a non-beverage single item is legitimate; missing beverage sizes and conflicting unit cells require review. Pack counts, flavours, editions, years, cans, kits and pods remain in the name. Only otherwise identical names after size removal and case/whitespace normalization are grouped. No spelling corrections or fuzzy merges are applied.", "",
        "Product/variant totals are proposals before ambiguity resolution. Same-price duplicate rows are linked with `duplicate_of`, retained in the CSV, and blocked from import pending explicit exclusion. Similar names are suggestions, including distinct flavours/strengths; they are never auto-merged.", "",
        "## Review and approval", "",
        "1. Confirm whether all 44 original categories should be imported.",
        "2. Review flagged rows against the original PDF/client. Correct data if needed; set `review_status` to `approved` or `exclude`. Unflagged rows use `ready`. Exclude redundant duplicate rows explicitly. Keep source fields intact for audit.",
        "3. Re-run the import dry-run on the reviewed CSV. Approval must refer to both exact file SHA-256 values. The write command requires `--approved-sha256` and `--approved-categories-sha256` matching the CSV and category manifest; pending rows block the entire import unless `--skip-pending` is explicitly chosen for a partial import.",
        "4. Only after user approval: apply the prepared schema migration, then execute the import. No migrations or writes were run during preparation. The updated model/admin code requires that migration before normal storefront/admin use; the import dry-run explicitly supports the old schema.", "",
        f"CSV SHA-256: `{digest}`", "",
        "## Categories", "", "| Order | Original category | Entries |", "|---:|---|---:|",
    ]
    md.extend(f"| {i} | {name} | {counts[name]} |" for i, name in enumerate(categories, 1))
    md += ["", "## Flags", "", "| Flag | Entries |", "|---|---:|"]
    md.extend(f"| {flag} | {count} |" for flag, count in sorted(flags.items()))
    md += ["", "## Duplicate groups", ""]
    md.extend(f"- {', '.join(group)}" for group in duplicates)
    md += ["", "## Rows requiring review", "", "| Row | PDF page / text line | Category | Source item | Size | Raw unit | KSh | Flags |", "|---|---|---|---|---|---|---:|---|"]
    for row in report["review_rows"]:
        values = [row["row_id"], f"{row['source_page']} / {row['source_line_start']}", row["category"], row["source_name"], row["size"], row["unit"], row["price_ksh"], row["flags"]]
        md.append("| " + " | ".join(str(v).replace("|", "\\|") for v in values) + " |")
    md += ["", "## Similar-name pairs", ""]
    md.extend(f"- {a} ↔ {b}" for a, b in near_matches)
    md += ["", "## Parse errors", "", json.dumps(errors, indent=2), ""]
    (out / "VALIDATION_REPORT.md").write_text("\n".join(md), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=SOURCE)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = export_catalogue(args.source, args.output_dir)
    print(json.dumps({k: v for k, v in report.items() if k not in {"review_rows", "similar_name_pairs", "category_counts"}}, indent=2))
