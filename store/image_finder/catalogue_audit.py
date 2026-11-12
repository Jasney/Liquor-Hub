"""Django-independent catalogue and staged-image audit primitives."""
import re
import zipfile
from difflib import SequenceMatcher
from xml.etree import ElementTree as ET

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def cell_value(cell, shared):
    if cell.attrib.get("t") == "s":
        value = cell.find("main:v", NS)
        return shared[int(value.text)] if value is not None else ""
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{NS['main']}}}t"))
    value = cell.find("main:v", NS)
    return value.text or "" if value is not None else ""


def workbook_products(path):
    with zipfile.ZipFile(path) as book:
        shared = []
        if "xl/sharedStrings.xml" in book.namelist():
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            shared = ["".join(n.text or "" for n in si.iter(f"{{{NS['main']}}}t")) for si in root]
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        relationships = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in relationships}
        sheet = next((s for s in workbook.findall("main:sheets/main:sheet", NS)
                      if s.attrib.get("name") == "All Products"), None)
        if sheet is None:
            raise ValueError("Workbook has no 'All Products' worksheet.")
        target = targets[sheet.attrib[f"{{{NS['rel']}}}id"]].lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        root = ET.fromstring(book.read(target))
    rows = root.findall("main:sheetData/main:row", NS)
    if not rows:
        return []

    def column_index(ref):
        letters = re.match(r"[A-Z]+", ref).group(0)
        value = 0
        for char in letters:
            value = value * 26 + ord(char) - 64
        return value - 1

    headers = {column_index(c.attrib["r"]): cell_value(c, shared) for c in rows[0].findall("main:c", NS)}
    products = []
    for row in rows[1:]:
        values = {headers.get(column_index(c.attrib["r"]), ""): cell_value(c, shared)
                  for c in row.findall("main:c", NS)}
        if values.get("Product Description", "").strip():
            products.append({
                "catalogue_row": int(row.attrib.get("r", len(products) + 2)),
                "section": values.get("Catalogue Section", ""),
                "category": values.get("Product Category", ""),
                "name": values.get("Product Description", "").strip(),
                "size": values.get("Size", "").strip(),
                "barcode": values.get("Bar Code", "").strip(),
                "filename": values.get("Image Filename", "").strip(),
                "alt_text": values.get("Website Alt Text", "").strip(),
            })
    return products


def normalized(value):
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"(\d+(?:\.\d+)?)\s*(?:millilit(?:er|re)s?|ml)\b", r"\1ml", value)
    value = re.sub(r"(\d+(?:\.\d+)?)\s*(?:litres?|liters?|l)\b", r"\1l", value)
    value = re.sub(r"(\d+)\s*(?:packs?|pk)\b", r"pack\1", value)
    return re.sub(r"[^a-z0-9%]+", " ", value).strip()


def tokens(value):
    ignored = {"the", "and", "of", "product", "photo", "image", "genuine", "piece"}
    return {token for token in normalized(value).split() if token not in ignored}


def normalized_size_values(value):
    result = set()
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(ml|l)\b", normalized(value)):
        result.add(float(number) * (1000 if unit == "l" else 1))
    return result


def multipacks(value):
    return set(re.findall(r"pack\d+|\d+pack|case\d+|\d+case", normalized(value)))


def rank_match(product, source_name, available_sizes=()):
    expected_name = tokens(product["name"])
    source_tokens = tokens(source_name)
    union = expected_name | source_tokens
    overlap = len(expected_name & source_tokens) / max(1, len(union))
    sequence = SequenceMatcher(None, normalized(product["name"]), normalized(source_name)).ratio()
    score = round((0.75 * overlap + 0.25 * sequence) * 100)
    expected_sizes = normalized_size_values(product["name"] + " " + product["size"])
    found_sizes = set()
    for size in list(available_sizes) + [source_name]:
        found_sizes.update(normalized_size_values(size))
    size_state = "matched" if expected_sizes and expected_sizes == found_sizes else "conflict" if expected_sizes and found_sizes else "unknown"
    expected_packs = multipacks(product["name"] + " " + product["size"])
    found_packs = multipacks(source_name)
    pack_state = "matched" if expected_packs and expected_packs <= found_packs else "conflict" if expected_packs and found_packs and not expected_packs & found_packs else "unknown"
    if size_state == "conflict":
        score = min(score, 45)
    if pack_state == "conflict":
        score = min(score, 35)
    if normalized(product["name"]) == normalized(source_name):
        score = max(score, 96 if size_state != "conflict" and pack_state != "conflict" else 45)
    return score, size_state, pack_state, f"name overlap {overlap:.2f}; size {size_state}; multipack {pack_state}"
