"""
Dimension & weight extraction utilities.

Shared by server.py and batch_publish.py to consistently extract
dimensions from GigaCloud product attributes.
"""
import re
from html import unescape
from typing import Optional, Dict, Any


def extract_numeric_lbs(text: str) -> Optional[float]:
    """Extract weight in pounds from various text formats.
    
    Handles: "180.77 lbs", "39.44lbs", "350 LBS", "123.20", 
             "37.92LBS/2PCS, 19.96LBS/PC"
    Returns None for non-numeric text like "Not specified", "N/A", etc.
    """
    if not text:
        return None
    text = str(text).strip()
    # Skip obvious non-numeric values
    if text.lower() in ('not specified', 'n/a', 'na', 'none', '-', ''):
        return None
    # Direct number
    try:
        v = float(text)
        if v > 0:
            return round(v, 2)
    except (ValueError, TypeError):
        pass
    # "123.20 LBS" / "39.44lbs" / "350 LBS"
    m = re.search(r'([\d.]+)\s*(?:lbs?|pounds?)', text, re.IGNORECASE)
    if m:
        return round(float(m.group(1)), 2)
    # "37.92LBS/2PCS" → first number
    m = re.search(r'([\d.]+)\s*(?:lbs?)', text, re.IGNORECASE)
    if m:
        return round(float(m.group(1)), 2)
    # Pure numeric
    m = re.match(r'^([\d.]+)$', text.strip())
    if m:
        return round(float(m.group(1)), 2)
    return None


def extract_numeric_inches(text: str) -> Optional[float]:
    """Extract dimension in inches from various text formats.
    
    Handles: "82.0", "32 in", '28.7"', "82.0 inches"
    """
    if not text:
        return None
    text = str(text).strip()
    try:
        v = float(text)
        if v > 0:
            return round(v, 1)
    except (ValueError, TypeError):
        pass
    m = re.search(r'([\d.]+)\s*(?:in|inch|")', text, re.IGNORECASE)
    if m:
        return round(float(m.group(1)), 1)
    m = re.match(r'^([\d.]+)$', text.strip())
    if m:
        return round(float(m.group(1)), 1)
    return None


def _clean_text(text: str) -> str:
    """Normalize HTML/text blobs before regex extraction."""
    if not text:
        return ""
    text = unescape(str(text))
    text = text.replace("''", '"')
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace("\xa0", " ")
    text = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def extract_product_dimensions_from_text(text: str) -> Dict[str, Optional[float]]:
    """Extract product/assembled dimensions from free text.

    Ignores packaging-oriented labels as much as possible.
    """
    cleaned = _clean_text(text)
    result = {"length": None, "width": None, "height": None}
    if not cleaned:
        return result

    contextual_patterns = [
        (
            r'(?:product|overall|item)\s+dimensions?\s*[:：]?\s*'
            r'(\d+\.?\d*)\s*(?:["”]|in(?:ches?)?)?\s*(?:\([lL]\))?\s*[x×]\s*'
            r'(\d+\.?\d*)\s*(?:["”]|in(?:ches?)?)?\s*(?:\([wW]\))?\s*[x×]\s*'
            r'(\d+\.?\d*)\s*(?:["”]|in(?:ches?)?)?\s*(?:\([hH]\))?'
        ),
        (
            r'overall\s+product\s+dimensions?\s*[:：]?\s*'
            r'(\d+\.?\d*)\s*(?:["”])?\s*[lL]\s*[x×]\s*'
            r'(\d+\.?\d*)\s*(?:["”])?\s*[wW]\s*[x×]\s*'
            r'\(?([\d.\s\-]+)\)?\s*(?:["”])?\s*[hH]'
        ),
        (
            r'产品尺寸\s*[:：]?\s*'
            r'(\d+\.?\d*)\s*(?:["”]|英寸|inch|inches?)?\s*(?:\([lL]\))?\s*[x×]\s*'
            r'(\d+\.?\d*)\s*(?:["”]|英寸|inch|inches?)?\s*(?:\([wW]\))?\s*[x×]\s*'
            r'(\d+\.?\d*)\s*(?:["”]|英寸|inch|inches?)?\s*(?:\([hH]\))?'
        ),
    ]
    for pattern in contextual_patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            result["length"] = round(float(match.group(1)), 1)
            result["width"] = round(float(match.group(2)), 1)
            third_group = match.group(3)
            third_numbers = [float(n) for n in re.findall(r'\d+\.?\d*', third_group)]
            if not third_numbers:
                continue
            result["height"] = round(max(third_numbers), 1)
            return result

    labeled_match = re.search(
        r'(?:overall\s+product\s+dimensions?|dimension\s*\(\s*overall\s*\)|overall\s+dimensions?)\s*[:：]?\s*(.{3,200})',
        cleaned,
        re.IGNORECASE,
    )
    if labeled_match:
        labeled_segment = labeled_match.group(1)
        triplet = re.search(
            r'(\d+\.?\d*)\s*(?:["”]|in(?:ches?)?)?\s*(?:[lwd])?\s*[x×]\s*'
            r'(\d+\.?\d*)\s*(?:["”]|in(?:ches?)?)?\s*(?:[lwd])?\s*[x×]\s*'
            r'(\d+\.?\d*)\s*(?:["”]|in(?:ches?)?)?\s*h?',
            labeled_segment,
            re.IGNORECASE,
        )
        if triplet:
            result["length"] = round(float(triplet.group(1)), 1)
            result["width"] = round(float(triplet.group(2)), 1)
            result["height"] = round(float(triplet.group(3)), 1)
            return result

        leading_dimension_block = re.split(
            r'(?:product\s+weight|item\s+weight|net\s+weight|package\s+dimension|package\s+weight)',
            labeled_segment,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        numbers = [float(n) for n in re.findall(r'\d+\.?\d*', leading_dimension_block)]
        if len(numbers) >= 3:
            result["length"] = round(numbers[0], 1)
            result["width"] = round(numbers[1], 1)
            result["height"] = round(max(numbers[2:]), 1)
            return result

    generic_triplet = re.search(
        r'(\d+\.?\d*)\s*(?:["”]|in(?:ches?)?)?\s*(?:[lwd])?\s*[x×]\s*'
        r'(\d+\.?\d*)\s*(?:["”]|in(?:ches?)?)?\s*(?:[lwd])?\s*[x×]\s*'
        r'(\d+\.?\d*)\s*(?:["”]|in(?:ches?)?)?\s*h?',
        cleaned,
        re.IGNORECASE,
    )
    if generic_triplet:
        result["length"] = round(float(generic_triplet.group(1)), 1)
        result["width"] = round(float(generic_triplet.group(2)), 1)
        result["height"] = round(float(generic_triplet.group(3)), 1)
        return result

    # Fallback for descriptions that mention "assembled" dimensions separately.
    axes = {
        "length": [
            r'assembled\s+length\s*\(?in\.?\)?\s*[:：]\s*(\d+\.?\d*)',
            r'组装长度\s*\(?英寸?\)?\s*[:：]\s*(\d+\.?\d*)',
        ],
        "width": [
            r'assembled\s+width\s*\(?in\.?\)?\s*[:：]\s*(\d+\.?\d*)',
            r'组装宽度\s*\(?英寸?\)?\s*[:：]\s*(\d+\.?\d*)',
        ],
        "height": [
            r'assembled\s+height\s*\(?in\.?\)?\s*[:：]\s*(\d+\.?\d*)',
            r'组装高度\s*\(?英寸?\)?\s*[:：]\s*(\d+\.?\d*)',
        ],
    }
    for axis, patterns in axes.items():
        for pattern in patterns:
            match = re.search(pattern, cleaned, re.IGNORECASE)
            if match:
                result[axis] = round(float(match.group(1)), 1)
                break
    return result


def extract_product_weight_from_text(text: str) -> Optional[float]:
    """Extract actual product/net/item weight from free text.

    Explicitly avoids package/gross/shipping/capacity weights.
    """
    cleaned = _clean_text(text)
    if not cleaned:
        return None

    patterns = [
        r'(?:product|item|net)\s+weight\s*[:：]\s*(\d+\.?\d*)\s*(?:lbs?|pounds?)',
        r'overall\s+product\s+weight(?:\s*\([^)]*\))?\s*[:：]?\s*(\d+\.?\d*)\s*(?:lbs?|pounds?)',
        r'产品重量\s*\(?磅?\)?\s*[:：]\s*(\d+\.?\d*)',
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            weight = round(float(match.group(1)), 2)
            if 0 < weight < 2000:
                return weight
    return None


def extract_dajian_measurements(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Extract trusted product/package measurements from a Dajian detail payload."""
    detail = detail or {}
    description = detail.get("description") or ""
    desc_dims = extract_product_dimensions_from_text(description)
    desc_weight = extract_product_weight_from_text(description)

    measurements = {
        "length": extract_numeric_inches(detail.get("length")),
        "width": extract_numeric_inches(detail.get("width")),
        "height": extract_numeric_inches(detail.get("height")),
        "packageWeight": extract_numeric_lbs(detail.get("weight")),
        "assembledLength": extract_numeric_inches(detail.get("assembledLength")) or desc_dims.get("length"),
        "assembledWidth": extract_numeric_inches(detail.get("assembledWidth")) or desc_dims.get("width"),
        "assembledHeight": extract_numeric_inches(detail.get("assembledHeight")) or desc_dims.get("height"),
        "productWeight": None,
        "weightUnit": detail.get("weightUnit", "lb"),
        "lengthUnit": detail.get("lengthUnit", "in"),
        "overSizeFlag": detail.get("overSizeFlag", False),
        "description": description,
    }

    for field in ("productWeight", "assembledWeight", "netWeight", "itemWeight"):
        value = extract_numeric_lbs(detail.get(field))
        if value:
            measurements["productWeight"] = value
            break

    if measurements["productWeight"] is None and desc_weight:
        measurements["productWeight"] = desc_weight

    return measurements


def source_description_marks_dimensions_unavailable(description: str) -> bool:
    """Return True when supplier copy explicitly says product dimensions are unavailable."""
    if not description:
        return False
    patterns = (
        r'组装长度\s*\(英寸\)\s*:\s*</span>\s*<span[^>]*>\s*Not Applicable\s*</span>',
        r'组装宽度\s*\(英寸\)\s*:\s*</span>\s*<span[^>]*>\s*Not Applicable\s*</span>',
        r'组装高度\s*\(英寸\)\s*:\s*</span>\s*<span[^>]*>\s*Not Applicable\s*</span>',
        r'Overall\s+Dimensions\s*\(L.?W.?H\)\s*</td>\s*<td[^>]*>\s*(?:Not Applicable|Not specified|NOT AVAILABLE)\s*</td>',
    )
    matches = sum(1 for pattern in patterns if re.search(pattern, description, flags=re.IGNORECASE | re.DOTALL))
    return matches >= 2


def replace_description_measurements(
    description: str,
    length: Optional[float] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    weight: Optional[float] = None,
    dimensions_text: Optional[str] = None,
    weight_text: Optional[str] = None,
) -> str:
    """Replace description measurement placeholders/rows with trusted values."""
    if not description:
        return description

    updated = description
    dimension_placeholder_pattern = re.compile(
        r'<tr[^>]*>\s*<td[^>]*>\s*Overall\s+Dimensions\s*\(L.?W.?H\)\s*</td>\s*<td[^>]*>\s*'
        r'(?:Not specified|Not Applicable|N/A|See Description|NOT AVAILABLE)[^<]*'
        r'</td>\s*</tr>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    chinese_dimension_placeholder_pattern = re.compile(
        r'<div[^>]*>\s*<span[^>]*>\s*组装(?:长度|宽度|高度)\s*\(英寸\)\s*:\s*</span>\s*'
        r'<span[^>]*>\s*(?:Not specified|Not Applicable|N/A|See Description|NOT AVAILABLE)\s*</span>\s*</div>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    chinese_weight_placeholder_pattern = re.compile(
        r'<div[^>]*>\s*<span[^>]*>\s*产品重量\s*\(磅\)\s*:\s*</span>\s*'
        r'<span[^>]*>\s*(?:Not specified|Not Applicable|N/A|See Description|NOT AVAILABLE)\s*</span>\s*</div>',
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _normalized_contains(html: str, snippet: str) -> bool:
        html_norm = re.sub(r'\s+', ' ', (html or '').lower().replace('×', 'x'))
        snippet_norm = re.sub(r'\s+', ' ', (snippet or '').lower().replace('×', 'x'))
        return bool(snippet_norm) and snippet_norm in html_norm

    def _replace_row(label_pattern: str, value: str, html: str, preserve_suffix_if_na: bool = False) -> str:
        pattern = (
            rf'(<tr[^>]*>\s*<td[^>]*>\s*{label_pattern}\s*</td>\s*<td[^>]*>)'
            rf'(.*?)'
            rf'(</td>)'
        )
        def _row_replacer(match):
            replacement = value
            if preserve_suffix_if_na:
                current_value = re.sub(r'<[^>]+>', '', match.group(2) or '', flags=re.IGNORECASE).strip()
                cleaned_suffix = re.sub(
                    r'^\s*not\s+available\s*(?:&mdash;|&#8212;|[—–\-:])?\s*',
                    '',
                    current_value,
                    flags=re.IGNORECASE,
                ).strip()
                if cleaned_suffix and cleaned_suffix.lower() != current_value.lower():
                    replacement = cleaned_suffix
            return f"{match.group(1)}{replacement}{match.group(3)}"

        return re.sub(pattern, _row_replacer, html, count=1, flags=re.IGNORECASE | re.DOTALL)

    def _replace_placeholder_span(label_pattern: str, value: str, html: str) -> str:
        pattern = (
            rf'(<div[^>]*>\s*<span[^>]*>\s*{label_pattern}\s*</span>\s*<span[^>]*>)'
            rf'\s*(?:Not specified|Not Applicable|N/A|See Description|NOT AVAILABLE)\s*'
            rf'(</span>)'
        )
        return re.sub(
            pattern,
            lambda match: f"{match.group(1)}{value}{match.group(2)}",
            html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )

    if dimensions_text is None and length is not None and width is not None and height is not None:
        dimensions_text = f"{length} × {width} × {height} inches"
    elif dimensions_text is None and (
        dimension_placeholder_pattern.search(updated)
        or chinese_dimension_placeholder_pattern.search(updated)
    ):
        dimensions_text = "See product dimension image"

    if dimensions_text:
        updated = _replace_row(
            r'Overall\s+Dimensions\s*\(L.?W.?H\)',
            dimensions_text,
            updated,
            preserve_suffix_if_na=dimensions_text.lower() == "not specified",
        )
        for label_pattern in (
            r'组装长度\s*\(英寸\)\s*:',
            r'组装宽度\s*\(英寸\)\s*:',
            r'组装高度\s*\(英寸\)\s*:',
        ):
            updated = _replace_placeholder_span(label_pattern, dimensions_text, updated)
        updated = re.sub(
            r'title="(?:Not specified|Not Applicable|N/A|See Description|NOT AVAILABLE)"',
            f'title="{dimensions_text}"',
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r'NOT AVAILABLE',
            dimensions_text,
            updated,
            count=1,
            flags=re.IGNORECASE,
        ) if "Overall Dimensions" in updated and "NOT AVAILABLE" in updated else updated

    if weight_text is None and weight is not None:
        weight_text = f"{weight} lbs"
    elif weight_text is None and (
        dimensions_text == "See product dimension image"
        or chinese_weight_placeholder_pattern.search(updated)
    ):
        weight_text = "See product dimension image"
    if weight_text:
        updated = _replace_row(
            r'(?:(?:Overall|Item|Product)\s+)?Weight(?:\s*\([^)]*\))?',
            weight_text,
            updated,
            preserve_suffix_if_na=weight_text.lower() == "not specified",
        )
        updated = _replace_placeholder_span(r'产品重量\s*\(磅\)\s*:', weight_text, updated)
        updated = re.sub(
            r'(<span[^>]*>\s*产品重量\s*\(磅\)\s*:\s*</span>\s*<span[^>]*>)'
            r'\s*(?:Not specified|Not Applicable|N/A|See Description|NOT AVAILABLE)\s*'
            r'(</span>)',
            # \g<1> not \1: a numeric value like "19.7 lbs" after \1 parses as
            # group \19 and raises "invalid group reference" (hit W2699P504456).
            rf'\g<1>{weight_text}\g<2>',
            updated,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )

    appended_blocks = []
    if dimensions_text and not _normalized_contains(updated, dimensions_text):
        appended_blocks.append(f"<p><strong>Product Dimensions:</strong> {dimensions_text}</p>")
    if weight_text and not _normalized_contains(updated, weight_text):
        appended_blocks.append(f"<p><strong>Item Weight:</strong> {weight_text}</p>")

    if appended_blocks:
        measurement_block = (
            '<div style="margin-top:16px;padding:14px 16px;background:#f8f9fa;border:1px solid #e5e7eb;">'
            + "".join(appended_blocks)
            + "</div>"
        )
        if re.search(r'</div>\s*$', updated, flags=re.IGNORECASE):
            updated = re.sub(
                r'(</div>\s*)$',
                measurement_block + r'\1',
                updated,
                count=1,
                flags=re.IGNORECASE | re.DOTALL,
            )
        else:
            updated += measurement_block

    return updated


def insert_dimension_note_row(description: str, note_text: str) -> str:
    """Insert a Dimension Note row after the Overall Dimensions row when absent."""
    if not description or not note_text:
        return description
    if "Dimension Note" in description or note_text in description:
        return description

    row_html = (
        '<tr style="background:#fdf8e8">'
        '<td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72">Dimension Note</td>'
        f'<td style="padding:10px;border-bottom:1px solid #e0e0e0;font-weight:500">{note_text}</td>'
        '</tr>'
    )

    return re.sub(
        r'(<tr[^>]*>\s*<td[^>]*>\s*Overall\s+Dimensions\s*\(L.?W.?H\)\s*</td>\s*<td[^>]*>.*?</td>\s*</tr>)',
        r'\1' + row_html,
        description,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )


def replace_description_weight_placeholder_with_package_weight(
    description: str,
    package_weight: Optional[float],
) -> str:
    """Replace ambiguous weight placeholder rows with explicit package weight rows."""
    if not description or not package_weight:
        return description

    weight_text = f"{round(package_weight, 2):g} lbs"

    def _row_replacer(match: re.Match) -> str:
        return f"{match.group(1)}Package Weight{match.group(3)}{weight_text}{match.group(4)}"

    return re.sub(
        r'(<tr[^>]*>\s*<td\b[^>]*>\s*)'
        r'((?:(?:Overall|Item|Product)\s+)?Weight(?:\s*\([^)]*\))?)'
        r'(\s*</td>\s*<td\b[^>]*>)'
        r'(?:Not specified|Not Applicable|N/A|See Description)[^<]*'
        r'(\s*</td>\s*</tr>)',
        _row_replacer,
        description,
        flags=re.IGNORECASE,
    )


def find_weight(attrs: Dict[str, Any]) -> Optional[float]:
    """Find product weight from attributes dict (tries multiple keys)."""
    weight_keys = [
        'Weight of Overrall Product',
        'Weight of Overall Product',
        'Overall Product Weight',
        'Overall Product Weight (with cushion)',
        'Product Weight (lbs.)',
        'Product Weight',
        'Net Weight',
        'Net  Weight',       # double-space variant
        'Overall Weight',
        'Weight',
        'Item Weight',
    ]
    for key in weight_keys:
        val = attrs.get(key)
        if val:
            w = extract_numeric_lbs(str(val))
            if w and w > 0:
                return w
    return None


def find_dimension(attrs: Dict[str, Any], dim_type: str) -> Optional[float]:
    """Find a dimension (Length/Width/Height) from attributes dict.
    
    Args:
        attrs: Product attributes dictionary
        dim_type: 'Length' | 'Width' | 'Height'
    """
    dim_keys = [
        f'Assembled {dim_type} (in.)',
        f'Overall {dim_type}',
        f'Product {dim_type}',
        f'{dim_type}',
    ]
    for key in dim_keys:
        val = attrs.get(key)
        if val:
            d = extract_numeric_inches(str(val))
            if d and 0 < d <= 500:
                return d
    return None


def extract_all_dimensions(attrs: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Extract all dimensions and weight from product attributes.
    
    Returns:
        Dict with keys: length, width, height, weight (values in inches/lbs, or None)
    """
    dims = {
        'length': find_dimension(attrs, 'Length'),
        'width': find_dimension(attrs, 'Width'),
        'height': find_dimension(attrs, 'Height'),
        'weight': find_weight(attrs),
    }
    if not all(dims.get(axis) for axis in ('length', 'width', 'height')):
        product_dimensions = attrs.get('Product Dimensions')
        parsed = extract_product_dimensions_from_text(product_dimensions)
        for axis in ('length', 'width', 'height'):
            if dims.get(axis) is None and parsed.get(axis) is not None:
                dims[axis] = parsed[axis]
    return normalize_dimension_orientation(attrs, dims)


def normalize_dimension_orientation(
    attrs: Dict[str, Any],
    dims: Dict[str, Optional[float]],
) -> Dict[str, Optional[float]]:
    """Correct known supplier orientation swaps before writing item specifics."""
    normalized = dict(dims or {})
    attrs = attrs if isinstance(attrs, dict) else {}
    descriptor = " ".join(
        str(attrs.get(key) or "")
        for key in ("Product Type", "Type", "Category", "Product Name", "Title")
    ).lower()

    length = normalized.get("length")
    width = normalized.get("width")
    height = normalized.get("height")

    # Some hall tree feeds use Length as vertical height and Height as depth
    # (for example 76.7"L x 59"W x 15.7"H). eBay's Item Height should be the
    # vertical dimension for these tall entryway pieces.
    if (
        "hall tree" in descriptor
        and length
        and width
        and height
        and length >= 60
        and width >= 30
        and height <= 30
    ):
        normalized["length"] = height
        normalized["width"] = width
        normalized["height"] = length

    return normalized


def populate_dimension_aspects(aspects: Dict[str, list], attrs: Dict[str, Any]) -> Dict[str, list]:
    """Add/override Item Length/Width/Height/Weight in aspects from product attributes.
    
    OVERWRITES existing values when source attributes have real data,
    since source attributes are more reliable than AI-generated values.
    Only skips overwrite if source data is missing.
    
    Returns:
        Dict of aspects that were added or updated
    """
    dims = extract_all_dimensions(attrs)
    
    mapping = {
        'Item Length': ('length', 'in'),
        'Item Width': ('width', 'in'),
        'Item Height': ('height', 'in'),
        'Item Weight': ('weight', 'lbs'),
    }
    
    updated = {}
    for aspect_key, (dim_key, unit) in mapping.items():
        val = dims[dim_key]
        if val:
            new_val = f"{val} {unit}"
            old_val = aspects.get(aspect_key, [None])[0] if aspects.get(aspect_key) else None
            if old_val != new_val:
                if old_val:
                    print(f"   [DIM-FIX] {aspect_key}: '{old_val}' -> '{new_val}' (from source attributes)")
                else:
                    print(f"   [DIM-ADD] {aspect_key}: '{new_val}' (from source attributes)")
            aspects[aspect_key] = [new_val]
            updated[aspect_key] = new_val
    
    return updated


def build_package_weight_and_size(
    attrs: Dict[str, Any],
    specs: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Build eBay ``packageWeightAndSize`` from product attributes & specs.

    Uses only explicit *package/shipping* dimensions & weight. Returns
    ``None`` when no trustworthy shipping data is available.

    Returns a dict ready to be merged into the inventory-item payload, e.g.::

        {
            "weight": {"value": 79.37, "unit": "POUND"},
            "dimensions": {"length": 46.0, "width": 15.7, "height": 55.12, "unit": "INCH"},
        }
    """
    specs = specs or {}
    result: Dict[str, Any] = {}

    # ── Weight (package/shipping only; never fall back to product weight) ──
    weight_keys = [
        'Package Weight (lbs.)',
        'Package Weight',
        'Shipping Weight',
        'Gross Weight',
    ]
    weight = None
    for key in weight_keys:
        val = specs.get(key) or attrs.get(key)
        if val:
            text = str(val)
            total_match = re.search(r'total\s*[:：]?\s*([\d.]+)\s*(?:lbs?|pounds?)', text, re.IGNORECASE)
            if total_match:
                w = round(float(total_match.group(1)), 2)
            else:
                w = extract_numeric_lbs(text)
            if w and 0 < w < 2000:
                weight = w
                break
    if weight and 0 < weight < 2000:
        result['weight'] = {'value': round(weight, 2), 'unit': 'POUND'}

    # ── Dimensions (package/shipping only) ──
    dims: Dict[str, float] = {}
    pkg_dim_keys = [
        ('length', ['Package Length (in.)', 'Shipping Length', 'Carton Length']),
        ('width',  ['Package Width (in.)', 'Shipping Width', 'Carton Width']),
        ('height', ['Package Height (in.)', 'Shipping Height', 'Carton Height']),
    ]
    for axis, spec_keys in pkg_dim_keys:
        for key in spec_keys:
            val = specs.get(key) or attrs.get(key)
            if val:
                d = extract_numeric_inches(str(val))
                if d and 0 < d < 999:
                    dims[axis] = round(d, 2)
                    break

    if len(dims) == 3:
        result['dimensions'] = {**dims, 'unit': 'INCH'}

    return result if result else None
