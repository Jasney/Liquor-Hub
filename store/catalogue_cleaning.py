"""Prepare ready/review CSVs without importing Django or accessing a database.

Run ``python -m store.catalogue_cleaning``. Original extraction files are retained.
"""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .catalogue import FIELDS, normalized, normalized_size


DEFAULT_INPUT = Path(__file__).resolve().parent.parent / "catalogue_data/liquor_catalogue_2026.csv"
EXTRA_FIELDS = (
    "original_category", "original_product_name", "original_size", "original_unit",
    "original_price_ksh", "original_flags", "proposed_category", "proposed_product_name",
    "proposed_size", "proposed_unit", "proposed_price_ksh", "review_reasons",
    "resolution_notes", "record_type", "storefront_eligible", "handling_action",
)
OUTPUT_FIELDS = (*FIELDS, *EXTRA_FIELDS)
SERVICE_NAME = re.compile(r"^(?:Delivery\b|courier$)", re.I)
RESOLVABLE_FLAGS = {
    "similar_name_review", "category_mismatch_possible", "nonstandard_extra_or_service",
    "nonstandard_accessory_or_service", "non_alcoholic_in_sparkling_wine",
}
REASONS = {
    "size_column_litre_conflict": "The table SIZE cell says Litre/1 Litre, conflicting with or not establishing the item's explicit capacity. Prefer the name's explicit size only as a review candidate; confirm the table value.",
    "size_column_piece_conflict": "The capacity embedded in the table unit differs from the item name. Confirm the explicit-name candidate before import.",
    "missing_size": "No explicit beverage size is present in the item name. Leave size blank; obtain confirmation rather than infer it from the packaging unit.",
    "possible_ocr_IL_size": "The source reads IL, which may be an OCR error. Confirm manually; no 1L size has been assigned.",
    "possible_name_alias": "Names may refer to the same product but are not identical. Confirm identity/grouping; no spelling correction or product merge has been applied.",
    "multiple_sizes_in_name": "More than one size was found in the name; confirm the intended sale variant.",
    "conflicting_duplicate_price": "Potentially identical items have different prices. Preserve both prices and obtain confirmation.",
}


def exact_identity(row):
    """No fuzzy matching, punctuation removal, unit conversion or flavour removal.

    Only case/spacing and ml/mls formatting are normalized. Unit and packaging
    must also match, so conflicting raw units cannot be silently deduplicated.
    """
    return (
        row["category"], normalized(row["product_name"]), normalized_size(row["size"]),
        normalized(row["unit"]), normalized(row["packaging"]), Decimal(row["price_ksh"]),
    )


def clean_rows(rows):
    ready, review, removed = [], [], []
    canonical = {}
    row_ids = set()
    duplicate_counts = Counter()
    prices_by_identity = {}
    for row in rows:
        if not set(FIELDS).issubset(row) or any(row[field] is None for field in FIELDS):
            continue
        try:
            price = Decimal(row["price_ksh"])
            if price.is_finite() and Decimal("0") < price <= Decimal("99999999.99") and price.as_tuple().exponent >= -2:
                identity = exact_identity(row)
                duplicate_counts[identity] += 1
                prices_by_identity.setdefault(identity[:-1], set()).add(price)
        except (InvalidOperation, ValueError):
            pass
    for original in rows:
        if not original.get("row_id") or original["row_id"] in row_ids:
            raise ValueError("Source row IDs must be nonempty and unique.")
        row_ids.add(original["row_id"])
        if not set(FIELDS).issubset(original) or any(original[field] is None for field in FIELDS):
            raise ValueError(f"Malformed source row: {original['row_id']}")
        row = dict(original)
        for field in ("category", "product_name", "size", "unit", "price_ksh", "flags"):
            row[f"original_{field}"] = original[field]
        for field in ("category", "product_name", "size", "unit", "price_ksh"):
            row[f"proposed_{field}"] = original[field]
        flags = set(filter(None, original["flags"].split(";")))
        notes = []
        reasons = []
        row.update(record_type="physical_product", storefront_eligible="true", handling_action="import_after_approval")
        try:
            price = Decimal(row["price_ksh"])
            valid_price = price.is_finite() and Decimal("0") < price <= Decimal("99999999.99") and price.as_tuple().exponent >= -2
        except (InvalidOperation, ValueError):
            valid_price = False
        if valid_price:
            identity = exact_identity(row)
            if len(prices_by_identity[identity[:-1]]) > 1:
                flags.add("conflicting_duplicate_price")
            if identity in canonical:
                removed.append({
                    "removed_row_id": row["row_id"], "retained_row_id": canonical[identity],
                    "reason": "Exact identity/category/size/packaging/unit/price match; case/size spacing only normalized.",
                    "original_row": original,
                })
                continue
            canonical[identity] = row["row_id"]
        else:
            flags.add("invalid_price")
            reasons.append("Price is missing or invalid. Do not infer or replace it.")

        # Resolved duplicate detection is recomputed from all identity fields;
        # duplicate_of from the older heuristic report is not trusted alone.
        proven_duplicate_group = valid_price and duplicate_counts[exact_identity(row)] > 1
        if proven_duplicate_group:
            flags.discard("duplicate_entry")
            row["duplicate_of"] = ""
            notes.append("Retained one canonical copy; only exact duplicate copies are removed and audited.")

        for flag in sorted(flags & RESOLVABLE_FLAGS):
            if flag == "similar_name_review":
                notes.append("Keep each distinct source name as a separate product; do not merge flavours, editions, colours, strengths, kits, pods or packs.")
            else:
                notes.append("Retain the original source category; no category correction is approved or applied.")
        flags -= RESOLVABLE_FLAGS

        if SERVICE_NAME.search(row["source_name"]):
            flags.discard("service_listing_review")
            flags.add("checkout_service_configuration_required")
            row.update(record_type="checkout_service", storefront_eligible="false", handling_action="configure_service_after_review")
            reasons.append("Delivery/courier charge: retain its original label and price as service/checkout configuration data. Do not create a physical Product or ProductVariant; service rules need confirmation.")
        elif normalized(row["source_name"]) == "miscellaneous sales":
            flags.discard("service_listing_review")
            flags.add("miscellaneous_sales_excluded_pending_review")
            row.update(record_type="excluded_pending_review", storefront_eligible="false", handling_action="exclude_from_storefront_pending_review")
            reasons.append("Miscellaneous Sales is excluded from the storefront pending clarification; preserve its original Meters unit and price.")

        if not valid_price:
            flags.add("invalid_price")
        if not row["category"] or not row["product_name"]:
            flags.add("missing_identity")
        for flag in sorted(flags):
            if flag in REASONS:
                reasons.append(REASONS[flag])
            elif flag not in {"invalid_price", "checkout_service_configuration_required", "miscellaneous_sales_excluded_pending_review"}:
                reasons.append(f"Unresolved source validation flag: {flag}.")
        if flags:
            row["review_status"] = "pending"
            row["handling_action"] = row["handling_action"] if row["record_type"] != "physical_product" else "hold_for_manual_confirmation"
            row["storefront_eligible"] = "false"
        else:
            row["review_status"] = "ready"
        row["flags"] = ";".join(sorted(flags))
        row["review_reasons"] = " | ".join(dict.fromkeys(reasons))
        row["resolution_notes"] = " | ".join(dict.fromkeys(notes))
        (review if flags else ready).append(row)
    return ready, review, removed


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def prepare_cleaned_catalogue(source=DEFAULT_INPUT, output_dir=None):
    source = Path(source)
    out = Path(output_dir or source.parent)
    out.mkdir(parents=True, exist_ok=True)
    with source.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    ready, review, duplicates = clean_rows(rows)
    ready_path = out / "catalogue_cleaned.csv"
    review_path = out / "catalogue_needs_review.csv"
    write_csv(ready_path, ready)
    write_csv(review_path, review)
    physical_review = [r for r in review if r["record_type"] == "physical_product"]
    services = [r for r in review if r["record_type"] == "checkout_service"]
    excluded = [r for r in review if r["record_type"] == "excluded_pending_review"]
    report = {
        "status": "AWAITING USER APPROVAL — NO DATABASE IMPORT PERFORMED",
        "source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "cleaned_sha256": hashlib.sha256(ready_path.read_bytes()).hexdigest(),
        "review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
        "source_entries": len(rows), "source_categories": len({r["category"] for r in rows}),
        "ready_entries": len(ready), "ready_unique_products": len({r["product_key"] for r in ready}),
        "ready_unique_variants": len({r["variant_key"] for r in ready}),
        "review_entries": len(review), "review_physical_entries": len(physical_review),
        "review_unique_physical_products": len({r["product_key"] for r in physical_review}),
        "review_unique_physical_variants": len({r["variant_key"] for r in physical_review}),
        "checkout_services": len(services), "excluded_pending_review": len(excluded),
        "exact_duplicates_removed": len(duplicates), "duplicate_audit": duplicates,
        "review_flag_counts": dict(Counter(flag for r in review for flag in r["flags"].split(";"))),
        "ready_category_counts": dict(Counter(r["category"] for r in ready)),
        "review_category_counts": dict(Counter(r["category"] for r in review)),
    }
    assert len(rows) == len(ready) + len(review) + len(duplicates)
    (out / "CLEANING_REPORT.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    text = [
        "# Catalogue cleaning review", "", report["status"], "",
        f"- Source entries: **{len(rows)}** across **{report['source_categories']} original categories** (the source has 44, not 42).",
        f"- Ready: **{report['ready_unique_products']} unique products**, **{report['ready_unique_variants']} variants**, **{len(ready)} CSV entries**.",
        f"- Requires review: **{len(review)} entries**, including **{len(physical_review)} physical-product variants** in **{report['review_unique_physical_products']} product groups**, **{len(services)} delivery/courier services**, and **{len(excluded)} excluded Miscellaneous Sales entry**.",
        f"- Exact duplicate copies removed: **{len(duplicates)}**. Canonical copies remain. The original extraction is unchanged; full removed-row values are in CLEANING_REPORT.json.",
        f"- Reconciliation: **{len(ready)} ready + {len(review)} review + {len(duplicates)} removed duplicate = {len(rows)} original entries**.", "",
        "Product groups can have variants in both files, so product-group counts across files are not additive. Variant counts refer to the conservative source grouping; no uncertain aliases have been merged.", "",
        "## Applied rules", "",
        "- Explicit capacity/weight in the item name is the preferred size candidate. Conflicting table SIZE cells remain unchanged in unit/raw_size_column and original_unit. These rows stay pending; proposed_size records the candidate, not an approved correction.",
        "- Missing beverage sizes stay blank. Fireball Cinnamon IL is held for manual confirmation; 1L is not assigned.",
        "- Duplicate removal requires matching category, product identity, size, packaging, unit and exact decimal price. Case/spacing and ml/mls formatting are the only normalizations. Different prices, flavours, editions, colours, strengths and packaging are never merged.",
        "- Earlier similar-name alerts are resolved by keeping distinct names separate. Possible generic-name aliases remain pending because their identity/grouping is uncertain.",
        "- Category-only alerts are resolved by the explicit instruction to preserve source assignments. Vapes under Cigars and non-alcoholic items under Sparkling Wine retain those categories, with original_flags and resolution_notes as an audit trail. No category corrections are applied.",
        "- Delivery/courier charges are checkout_service records in the review file, not physical products. Their prices/labels are retained for future configuration, which is not implemented or activated here.",
        "- Miscellaneous Sales remains in the review file with storefront_eligible=false and exclusion instructions.",
        "- All valid numeric prices are copied without rounding, currency conversion or adjustment. raw_price retains the original formatted price string. Source names, raw units, PDF page/text lines, original fields, proposed fields and reasons are preserved.", "",
        "## Before import", "",
        "The cleaned CSV is compatible with the existing database-driven importer and editable Product/Variant/Category admin models. No fixtures or hardcoded storefront products are added. The importer rejects service/non-storefront rows and still requires exact-file approval digests for both the cleaned CSV and unchanged categories_2026.csv manifest. New variants default to zero stock; images and existing client edits are preserved.", "",
        "Run a read-only plan with:", "", "```bash",
        "python manage.py import_liquor_catalog --csv catalogue_data/catalogue_cleaned.csv --dry-run", "```", "",
        "Wait for the user's approval before importing. Resolving a row later requires checking its original values and proposed changes, updating its data and review status explicitly, and generating a new approval digest. Do not directly import the review file as products.", "",
        "The source text is available; the original PDF was not supplied for visual table verification. Regenerating these outputs resets their manual review edits; preserve reviewed copies separately.", "",
        "## Exact duplicate audit", "",
    ]
    for duplicate in duplicates:
        row = duplicate["original_row"]
        text.append(f"- Removed {duplicate['removed_row_id']}; retained {duplicate['retained_row_id']}: {row['source_name']} / {row['category']} / {row['unit']} / KSh {row['raw_price']}.")
    text += ["", "## Unresolved reasons", "", "| Reason | Entries |", "|---|---:|"]
    text.extend(f"| {flag} | {count} |" for flag, count in sorted(report["review_flag_counts"].items()))
    text += ["", f"Cleaned CSV SHA-256: `{report['cleaned_sha256']}`", "", f"Review CSV SHA-256: `{report['review_sha256']}`", ""]
    (out / "CLEANING_REPORT.md").write_text("\n".join(text), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = prepare_cleaned_catalogue(args.source, args.output_dir)
    print(json.dumps({k: v for k, v in report.items() if k not in {"duplicate_audit", "ready_category_counts", "review_category_counts"}}, indent=2))
