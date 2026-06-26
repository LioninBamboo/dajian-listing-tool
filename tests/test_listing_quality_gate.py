from src.utils.listing_quality_gate import (
    normalize_generated_listing,
    sanitize_generated_description_html,
    validate_listing_quality,
)

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_quality_gate_sanitizes_literal_escape_and_quote_artifacts():
    dirty = (
        '<div style="max-width:900px">\\n'
        '<div><p>PREMIUM HOME FURNISHINGS</p>"></div>'
        '<div>"><h2>Accent Chair</h2></div>'
        '</div>'
    )

    cleaned = sanitize_generated_description_html(dirty)

    assert "\\n" not in cleaned
    assert '</p>"></div>' not in cleaned
    assert '>"><h2>' not in cleaned
    assert "<h2>Accent Chair</h2>" in cleaned


def test_quality_gate_normalizes_patio_set_category_aspects_and_description_measurements():
    opt = {
        "title": "Luxury Patio Furniture Set with Removable Cushions Coffee Table L-Shaped Acacia",
        "description": "<div><p>Outdoor set.</p></div>",
        "categoryId": "38208",
        "categoryName": "Sofas, Armchairs & Couches",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Sofa"],
            "Set Includes": ["Coffee Table"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "89.4",
            "Assembled Width (in.)": "62.3",
            "Assembled Height (in.)": "31.9",
            "Product Weight (lbs.)": "113.54",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "139849"
    assert normalized["categoryName"] == "Patio & Garden Furniture Sets"
    assert normalized["aspects"]["Type"] == ["Patio Furniture Set"]
    assert normalized["aspects"]["Set Includes"] == ["Sofa Set"]
    assert normalized["aspects"]["Indoor/Outdoor"] == ["Outdoor"]
    assert normalized["aspects"]["Item Length"] == ["89.4 in"]
    assert "89.4" in normalized["description"]
    assert "62.3" in normalized["description"]
    assert "31.9" in normalized["description"]


def test_quality_gate_uses_source_assembly_required_over_ai_copy():
    opt = {
        "title": "Transformer Coffee Table 2 in 1 Extendable Dining Table Walnut",
        "description": (
            "<div><p>Space-Smart & Assembly-Free: Ships fully assembled - ready to use right out of the box. "
            "Saves time, eliminates frustration, and guarantees structural integrity.</p>"
            "<p>Expandable mechanism - no tools required.</p></div>"
        ),
        "categoryId": "38204",
        "categoryName": "Coffee Tables",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Coffee Table"],
            "Assembly Required": ["No"],
            "Packaging": ["Fully Assembled"],
        },
    }

    source_description = "<div>Product Size 31.5 x 27.5 x 16.1 in Assembly Required Yes</div>"
    normalized = normalize_generated_listing(
        opt,
        source_title="Transformer coffee table, 2 in 1 table, Expandable dining table",
        source_description=source_description,
        attributes={
            "Assembled Length (in.)": "31.50",
            "Assembled Width (in.)": "27.50",
            "Assembled Height (in.)": "16.10",
            "Product Weight (lbs.)": "52.80",
        },
        specs={},
        images=["img1", "img2"],
    )

    desc_lower = normalized["description"].lower()
    assert normalized["aspects"]["Assembly Required"] == ["Yes"]
    assert "Packaging" not in normalized["aspects"]
    assert "assembly-free" not in desc_lower
    assert "fully assembled" not in desc_lower
    assert "right out of the box" not in desc_lower

    issues = validate_listing_quality(
        normalized,
        source_title="Transformer coffee table, 2 in 1 table, Expandable dining table",
        source_description=source_description,
        attributes={
            "Assembled Length (in.)": "31.50",
            "Assembled Width (in.)": "27.50",
            "Assembled Height (in.)": "16.10",
            "Product Weight (lbs.)": "52.80",
        },
        specs={},
        images=["img1", "img2"],
    )
    assert not any(issue.code.startswith("assembly_") for issue in issues)


def test_quality_gate_blocks_assembly_required_mismatch_before_publish():
    opt = {
        "title": "Transformer Coffee Table 2 in 1 Extendable Dining Table Walnut",
        "description": "<div>No assembly required. Ships fully assembled.</div>",
        "categoryId": "38204",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Coffee Table"],
            "Assembly Required": ["No"],
            "Item Length": ["31.5 in"],
            "Item Width": ["27.5 in"],
            "Item Height": ["16.1 in"],
            "Packaging": ["Fully Assembled"],
        },
    }

    issues = validate_listing_quality(
        opt,
        source_title="Transformer coffee table",
        source_description="<div>Assembly Required Yes</div>",
        attributes={
            "Assembled Length (in.)": "31.50",
            "Assembled Width (in.)": "27.50",
            "Assembled Height (in.)": "16.10",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert {issue.code for issue in issues} >= {
        "assembly_required_mismatch",
        "assembly_description_contradiction",
        "assembly_packaging_contradiction",
    }


def test_quality_gate_does_not_flag_no_assembly_required_as_contradiction():
    opt = {
        "title": "Round Storage Ottoman with Wood Legs",
        "description": "<div>No Assembly Required: Ships fully assembled-unbox, place, and enjoy.</div>",
        "categoryId": "20490",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Storage Ottoman"],
            "Assembly Required": ["No"],
            "Item Length": ["20 in"],
            "Item Width": ["20 in"],
            "Item Height": ["18 in"],
        },
    }

    issues = validate_listing_quality(
        opt,
        source_title="Round Storage Ottoman with Wood Legs",
        source_description="<div>Assembly Required No</div>",
        attributes={
            "Assembled Length (in.)": "20",
            "Assembled Width (in.)": "20",
            "Assembled Height (in.)": "18",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert not any(issue.code.startswith("assembly_") for issue in issues)


def test_quality_gate_does_not_treat_no_tools_as_no_assembly():
    yes_opt = {
        "title": "Twin Platform Bed Frame",
        "description": "<div>Effortless Assembly: requires assembly before use; no tools required beyond included Allen wrench.</div>",
        "categoryId": "175758",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Assembly Required": ["Yes"],
            "Item Length": ["80 in"],
            "Item Width": ["43 in"],
            "Item Height": ["41 in"],
        },
    }
    no_opt = {
        "title": "Compressed Sofa",
        "description": "<div>Expands to full size in minutes with no tools or assembly required.</div>",
        "categoryId": "38208",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Assembly Required": ["No"],
            "Item Length": ["116 in"],
            "Item Width": ["39 in"],
            "Item Height": ["29 in"],
        },
    }

    yes_issues = validate_listing_quality(
        yes_opt,
        source_title="Twin Platform Bed Frame",
        source_description="<div>Assembly Required Yes</div>",
        attributes={
            "Assembled Length (in.)": "80",
            "Assembled Width (in.)": "43",
            "Assembled Height (in.)": "41",
        },
        specs={},
        images=["img1", "img2"],
    )
    no_issues = validate_listing_quality(
        no_opt,
        source_title="Compressed Sofa",
        source_description="<div>Assembly Required No</div>",
        attributes={
            "Assembled Length (in.)": "116",
            "Assembled Width (in.)": "39",
            "Assembled Height (in.)": "29",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert not any(issue.code == "assembly_description_contradiction" for issue in yes_issues)
    assert not any(issue.code == "assembly_description_contradiction" for issue in no_issues)


def test_quality_gate_adds_missing_assembly_required_copy_and_removes_unsupported_status():
    normalized = normalize_generated_listing(
        {
            "title": "Heated Massage Swivel Recliner Chair with Ottoman",
            "description": (
                '<div><p>Comfortable swivel recliner chair with ottoman for daily use.</p>'
                '<h3>SPECIFICATIONS</h3><table>'
                '<tr><td>Overall Dimensions (L×W×H)</td><td>44.09 × 30.71 × 42.12 inches</td></tr>'
                '<tr style="background:#fafafa"><td>Weight</td><td>48.0 lbs</td></tr>'
                "</table></div>"
            ),
            "categoryId": "38208",
            "categoryName": "Sofas, Armchairs & Couches",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Accent Chair"],
                "Assembly Status": ["Part Assembled"],
            },
        },
        source_title="Heated Massage Swivel Recliner Chair with Ottoman",
        source_description="<div>Assembly Required Yes</div>",
        attributes={
            "Assembled Length (in.)": "44.09",
            "Assembled Width (in.)": "30.71",
            "Assembled Height (in.)": "42.12",
        },
        specs={},
        images=["img1", "img2"],
    )

    desc_lower = normalized["description"].lower()
    assert normalized["aspects"]["Assembly Required"] == ["Yes"]
    assert "Assembly Status" not in normalized["aspects"]
    assert "Overall Dimensions (L×W×H)" in normalized["description"]
    assert "Weight" in normalized["description"]
    assert "Assembly Required" in normalized["description"]
    assert normalized["description"].count("<tr") == 3
    assert "assembly required" in desc_lower
    assert "setup required before use" in desc_lower


def test_quality_gate_preserves_explicit_source_assembly_status():
    normalized = normalize_generated_listing(
        {
            "title": "Storage Bench",
            "description": "<div>Entryway bench with hidden storage.</div>",
            "categoryId": "20490",
            "categoryName": "Ottomans, Footstools & Poufs",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Storage Ottoman"],
                "Assembly Status": ["Part Assembled"],
            },
        },
        source_title="Storage Bench",
        source_description="<div>Assembly Required Yes. Ships partially assembled.</div>",
        attributes={},
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["aspects"]["Assembly Status"] == ["Part Assembled"]
    assert normalized["aspects"]["Assembly Required"] == ["Yes"]


def test_quality_gate_moves_plain_assembly_copy_into_specifications_row():
    normalized = normalize_generated_listing(
        {
            "title": "Garden Cart",
            "description": (
                '<div style="max-width:900px;">'
                '<p><strong>Assembly Required:</strong> Assembly is required before use. '
                "Follow the included instructions and hardware guide.</p>"
                "<h3>SPECIFICATIONS</h3><table>"
                '<tr><td>Weight</td><td>41.51 lbs</td></tr>'
                "</table></div>"
            ),
            "categoryId": "139939",
            "categoryName": "Greenhouses",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Assembly Required": ["Yes"],
            },
        },
        source_title="Garden Cart",
        source_description="<div>Assembly Required Yes</div>",
        attributes={},
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["description"].count("Assembly Required") == 1
    assert "<strong>Assembly Required:</strong>" not in normalized["description"]
    assert normalized["description"].count("<tr") == 2
    assert "Weight" in normalized["description"]


def test_quality_gate_strips_stray_div_closers_inside_specifications_table():
    normalized = normalize_generated_listing(
        {
            "title": "Storage Bed Frame",
            "description": (
                '<div><h3>SPECIFICATIONS</h3><table>'
                '<tr><td>Overall Dimensions (L×W×H)</td><td>77.0 × 53.5 × 43.4 inches</td></tr>'
                '<tr style="background:#fafafa"><td>Weight</td><td>125.66 lbs</td></tr>'
                '<tr><td>Material</td><td>Plywood + MDF + LVL</td></tr>'
                "</div></div></table></div>"
            ),
            "categoryId": "20487",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Assembly Required": ["Yes"],
            },
        },
        source_title="Storage Bed Frame",
        source_description="<div>Assembly Required Yes</div>",
        attributes={},
        specs={},
        images=["img1", "img2"],
    )

    desc = normalized["description"]
    assert "</div></div></table>" not in desc
    assert "Assembly Required" in desc
    specs_section = desc[desc.index("<h3>SPECIFICATIONS</h3>"):]
    assert "</div>" not in specs_section.split("</table>", 1)[0]


def test_quality_gate_sanitizes_specifications_table_even_without_assembly_rule():
    normalized = normalize_generated_listing(
        {
            "title": "Accent Cabinet",
            "description": (
                '<div><h3>SPECIFICATIONS</h3><table>'
                '<tr><td>Weight</td><td>72.64 lbs</td></tr></div></div></table></div>'
            ),
            "categoryId": "20487",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Assembly Required": ["No"],
            },
        },
        source_title="Accent Cabinet",
        source_description="<div>Assembly Required No</div>",
        attributes={},
        specs={},
        images=["img1", "img2"],
    )

    desc = normalized["description"]
    specs_section = desc[desc.index("<h3>SPECIFICATIONS</h3>"):]
    assert "</div>" not in specs_section.split("</table>", 1)[0]


def test_quality_gate_repairs_bench_and_rejects_table_or_pet_specifics():
    opt = {
        "title": "50 Inch Upholstered Long Bench Modern Dining Bench with Soft Chenille Fabric",
        "description": "<div>Bench description</div>",
        "categoryId": "20740",
        "categoryName": "Furniture & Scratchers",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Coffee Table"],
            "Set Includes": ["Table"],
            "Pet Type": ["Cat"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "50.25",
            "Assembled Width (in.)": "15.75",
            "Assembled Height (in.)": "18.50",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "262980"
    assert normalized["aspects"]["Type"] == ["Bench"]
    assert normalized["aspects"]["Set Includes"] == ["Bench"]
    assert "Pet Type" not in normalized["aspects"]


def test_quality_gate_cleans_furniture_specifics_and_mojibake():
    opt = {
        "title": "Round Storage Ottoman Boucl谷 Fabric Footstool 每 Navy",
        "description": "<div>Boucl谷 ottoman 每 hidden storage.</div>",
        "categoryId": "38204",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Coffee Table"],
            "US Shoe Size": ["8"],
            "Sport": ["Athletic"],
            "Top Material": ["Wood"],
            "Upholstery Material": ["Boucl谷"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "30",
            "Assembled Width (in.)": "30",
            "Assembled Height (in.)": "17.25",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "20490"
    assert "Boucl谷" not in normalized["title"]
    assert "每" not in normalized["description"]
    assert aspects["Type"] == ["Storage Ottoman"]
    assert aspects["Upholstery Material"] == ["Boucle"]
    assert "US Shoe Size" not in aspects
    assert "Sport" not in aspects
    assert "Top Material" not in aspects


def test_quality_gate_blocks_missing_dimensions_and_one_image():
    opt = {
        "title": "Set of 2 Mid Century Upholstered Dining Chairs",
        "description": "<div>Dining chairs</div>",
        "categoryId": "54235",
        "aspects": {
            "Brand": ["AquaVerve"],
            "US Shoe Size": ["10"],
            "Set Includes": ["Dining Table & Chairs"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={},
        specs={},
        images=["img1"],
    )
    issues = validate_listing_quality(
        normalized,
        source_title=opt["title"],
        attributes={},
        specs={},
        images=["img1"],
    )

    assert normalized["aspects"]["Set Includes"] == ["Chairs"]
    assert "US Shoe Size" not in normalized["aspects"]
    assert {issue.code for issue in issues} >= {
        "missing_measurement",
        "insufficient_images",
    }


def test_quality_gate_keeps_board_game_table_out_of_chair_category():
    opt = {
        "title": "Board Game Table with Removable Top Cup Holders for 4–6 Players 63x40in Rustic Brown",
        "description": "<div>Pairs with standard dining chairs for game night.</div>",
        "categoryId": "54235",
        "categoryName": "Chairs",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Dining Chair"],
            "Set Includes": ["Chairs"],
            "Game Type": ["Board Game"],
            "Game Title": ["Poker"],
            "Min. Number of Players": ["2 players"],
            "Upholstery Material": ["PVC"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title='63" x 40" Board Game Table with Removable Table Top for 4-6 Players',
        attributes={
            "Assembled Length (in.)": "62.99",
            "Assembled Width (in.)": "40.16",
            "Assembled Height (in.)": "33.46",
            "Product Weight (lbs.)": "121.92",
        },
        specs={},
        images=["img1", "img2"],
    )
    issues = validate_listing_quality(
        normalized,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "62.99",
            "Assembled Width (in.)": "40.16",
            "Assembled Height (in.)": "33.46",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "38204"
    assert normalized["categoryName"] == "Tables"
    assert "4-6 Players" in normalized["title"]
    assert "4–6" not in normalized["title"]
    assert aspects["Type"] == ["Game Table"]
    assert aspects["Set Includes"] == ["Table"]
    assert aspects["Room"] == ["Game Room"]
    assert "Game Type" not in aspects
    assert "Game Title" not in aspects
    assert "Min. Number of Players" not in aspects
    assert "Upholstery Material" not in aspects
    assert not issues


def test_quality_gate_keeps_ping_pong_table_out_of_dining_set_category():
    opt = {
        "title": "4.5ft Foldable Table Tennis Table Set with Net and 2 Paddles Black",
        "description": "<div>Portable ping pong table for indoor and outdoor play.</div>",
        "categoryId": "107578",
        "categoryName": "Dining Sets",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Dining Set"],
            "Set Includes": ["Dining Table & Chairs"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "53.9",
            "Assembled Width (in.)": "29.9",
            "Assembled Height (in.)": "26.4",
            "Product Weight (lbs.)": "30.82",
        },
        specs={},
        images=["img1", "img2"],
    )
    issues = validate_listing_quality(
        normalized,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "53.9",
            "Assembled Width (in.)": "29.9",
            "Assembled Height (in.)": "26.4",
            "Product Weight (lbs.)": "30.82",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "97075"
    assert normalized["categoryName"] == "Tables"
    assert aspects["Type"] == ["Table Tennis Table"]
    assert aspects["Set Includes"] == ["Table"]
    assert not any(issue.code == "category_mismatch" for issue in issues)


def test_quality_gate_keeps_coffee_table_set_out_of_dining_set_category():
    opt = {
        "title": "2-Piece Round Nesting Coffee Table Set Black Metal Modern Indoor Outdoor",
        "description": "<div>Nesting coffee table set for living room, bedroom, balcony, patio, indoor and outdoor use.</div>",
        "categoryId": "107578",
        "categoryName": "Dining Sets",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Dining Set"],
            "Set Includes": ["Dining Table & Chairs"],
            "Number of Items in Set": ["2"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "31.0",
            "Assembled Width (in.)": "31.0",
            "Assembled Height (in.)": "28.0",
        },
        specs={},
        images=["img1", "img2"],
    )
    issues = validate_listing_quality(
        normalized,
        source_title=opt["title"],
        source_description=opt["description"],
        attributes={
            "Assembled Length (in.)": "31.0",
            "Assembled Width (in.)": "31.0",
            "Assembled Height (in.)": "28.0",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "38204"
    assert normalized["categoryName"] == "Coffee Tables"
    assert aspects["Type"] == ["Coffee Table"]
    assert aspects["Set Includes"] == ["Table"]
    assert not any(issue.code == "category_mismatch" for issue in issues)


def test_quality_gate_keeps_bean_bag_chair_out_of_sofa_category():
    opt = {
        "title": "Giant Bean Bag Chair for Adults with Lumbar Pillow Oversized Corduroy Sofa Dark Grey",
        "description": "<div>Oversized lazy sofa style bean bag chair for living room.</div>",
        "categoryId": "38208",
        "categoryName": "Sofas, Armchairs & Couches",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Sofa"],
            "Set Includes": ["Sofa"],
            "Seating Capacity": ["1"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "39.37",
            "Assembled Width (in.)": "47.24",
            "Assembled Height (in.)": "29.92",
            "Product Weight (lbs.)": "36.16",
        },
        specs={},
        images=["img1", "img2"],
    )

    aspects = normalized["aspects"]
    assert normalized["categoryId"] == "48319"
    assert normalized["categoryName"] == "Bean Bags & Inflatables"
    assert aspects["Type"] == ["Beanbag"]
    assert "Set Includes" not in aspects


def test_quality_gate_classifies_modular_sectional_and_armchair_type():
    sectional = normalize_generated_listing(
        {
            "title": "Modern U-Shaped Modular Sectional Sofa for Living Room Corduroy Fabric 4 Seat",
            "description": "<div>Overall Dimensions (L x W x H): 151.9 x 69.7 x 27.1 inches</div>",
            "categoryId": "38208",
            "aspects": {"Brand": ["AquaVerve"], "Type": ["Sofa"]},
        },
        source_title="Modern U-Shaped Modular Sectional Sofa for Living Room Corduroy Fabric 4 Seat",
        attributes={
            "Assembled Length (in.)": "151.9",
            "Assembled Width (in.)": "69.7",
            "Assembled Height (in.)": "27.1",
        },
        specs={},
        images=["img1", "img2"],
    )
    armchair = normalize_generated_listing(
        {
            "title": "Comfy Deep Single Seat Sofa Upholstered Reading Armchair Living Room Chair Green Chenille",
            "description": "<div>Overall Dimensions (L x W x H): 40.9 x 34.2 x 33 inches</div>",
            "categoryId": "38208",
            "aspects": {"Brand": ["AquaVerve"], "Type": ["Sofa"]},
        },
        source_title="Comfy Deep Single Seat Sofa Upholstered Reading Armchair Living Room Chair Green Chenille",
        attributes={
            "Assembled Length (in.)": "40.9",
            "Assembled Width (in.)": "34.2",
            "Assembled Height (in.)": "33",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert sectional["aspects"]["Type"] == ["Sectional"]
    assert sectional["aspects"]["Set Includes"] == ["Sofa Set"]
    assert armchair["aspects"]["Type"] == ["Armchair"]
    assert armchair["aspects"]["Set Includes"] == ["Chair"]
    assert armchair["aspects"]["Color"] == ["Green"]


def test_quality_gate_keeps_sofa_with_storage_ottoman_out_of_ottoman_category():
    opt = {
        "title": '42" Oversized Single Sofa, Faux Rabbit Fur Armchair with Cup Holders, Upholstered Chaise Lounge with Storage Ottoman and Armrests',
        "description": "<div>Deep seat sofa chair with hidden side storage and wide ottoman-style chaise support.</div>",
        "categoryId": "20490",
        "categoryName": "Ottomans, Footstools & Poufs",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Storage Ottoman"],
            "Set Includes": ["Ottoman"],
        },
    }

    normalized = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        attributes={
            "Assembled Length (in.)": "42",
            "Assembled Width (in.)": "53",
            "Assembled Height (in.)": "33.5",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "38208"
    assert normalized["categoryName"] == "Sofas, Armchairs & Couches"
    assert normalized["aspects"]["Type"] == ["Armchair"]
    assert normalized["aspects"]["Set Includes"] == ["Chair"]


def test_quality_gate_handles_outdoor_daybed_and_outdoor_chair_categories():
    daybed = normalize_generated_listing(
        {
            "title": "Outdoor Acacia Wood Round Daybed, Patio Lounger with Curved Slatted Backrest and Cushion",
            "description": "<div>Round outdoor daybed for patio and poolside use.</div>",
            "categoryId": "175758",
            "categoryName": "Beds & Bedframes",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Outdoor Acacia Wood Round Daybed, Patio Lounger with Curved Slatted Backrest and Cushion",
        attributes={
            "Assembled Length (in.)": "59",
            "Assembled Width (in.)": "60.3",
            "Assembled Height (in.)": "25.5",
        },
        specs={},
        images=["img1", "img2"],
    )
    chair = normalize_generated_listing(
        {
            "title": "Set of 2 Outdoor Acacia Wood Club Chairs with Modern Square Arms, Patio Conversation Set",
            "description": "<div>Outdoor chair set for patio, backyard, deck, and poolside.</div>",
            "categoryId": "139849",
            "categoryName": "Patio & Garden Furniture Sets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Set of 2 Outdoor Acacia Wood Club Chairs with Modern Square Arms, Patio Conversation Set",
        attributes={
            "Assembled Length (in.)": "27.55",
            "Assembled Width (in.)": "26.77",
            "Assembled Height (in.)": "31.88",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert daybed["categoryId"] == "138996"
    assert daybed["categoryName"] == "Outdoor Daybeds"
    assert daybed["aspects"]["Indoor/Outdoor"] == ["Outdoor"]
    assert chair["categoryId"] == "79684"
    assert chair["categoryName"] == "Outdoor Chairs"
    assert chair["aspects"]["Indoor/Outdoor"] == ["Outdoor"]
    assert chair["aspects"]["Type"] == ["Outdoor Chair"]
    assert chair["aspects"]["Set Includes"] == ["Chairs"]


def test_quality_gate_routes_desk_and_golf_profiles_to_correct_categories():
    desk = normalize_generated_listing(
        {
            "title": "L Shaped Desk with Drawers Bookshelf LED Light Modern Corner Desk Home Office Desk",
            "description": "<div>Space-saving computer desk for home office.</div>",
            "categoryId": "3199",
            "categoryName": "Bookcases",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="L Shaped Desk with Drawers Bookshelf LED Light Modern Corner Desk Home Office Desk",
        attributes={
            "Assembled Length (in.)": "59.44",
            "Assembled Width (in.)": "48.6",
            "Assembled Height (in.)": "63.38",
        },
        specs={},
        images=["img1", "img2"],
    )
    putting_green = normalize_generated_listing(
        {
            "title": "12x5 FT Golf Putting Green, Golf Training Mat with Ball Return",
            "description": "<div>Indoor outdoor golf putting green mat.</div>",
            "categoryId": "20740",
            "categoryName": "Furniture & Scratchers",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="12x5 FT Golf Putting Green, Golf Training Mat with Ball Return",
        attributes={
            "Assembled Length (in.)": "141.73",
            "Assembled Width (in.)": "59.06",
            "Assembled Height (in.)": "1.97",
        },
        specs={},
        images=["img1", "img2"],
    )
    hitting_mat = normalize_generated_listing(
        {
            "title": "5x4 FT Golf Hitting Mat with Cross Stance Guide and 7 Tee Positions",
            "description": "<div>Heavy-duty golf swing mat for practice.</div>",
            "categoryId": "20740",
            "categoryName": "Furniture & Scratchers",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="5x4 FT Golf Hitting Mat with Cross Stance Guide and 7 Tee Positions",
        attributes={
            "Assembled Length (in.)": "59.06",
            "Assembled Width (in.)": "47.24",
            "Assembled Height (in.)": "1.57",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert desk["categoryId"] == "88057"
    assert desk["categoryName"] == "Desks & Tables"
    assert putting_green["categoryId"] == "36234"
    assert putting_green["categoryName"] == "Putting Greens & Aids"
    assert hitting_mat["categoryId"] == "50876"
    assert hitting_mat["categoryName"] == "Nets, Cages & Mats"


def test_quality_gate_routes_kids_table_sets_out_of_dining_category():
    normalized = normalize_generated_listing(
        {
            "title": "Kids Activity Table and Chair Set with Storage Bins Wooden Play Table",
            "description": "<div>Children play table and chair set for arts, crafts, and reading.</div>",
            "categoryId": "107578",
            "categoryName": "Dining Sets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Kids Activity Table and Chair Set with Storage Bins Wooden Play Table",
        attributes={
            "Assembled Length (in.)": "31.5",
            "Assembled Width (in.)": "23.6",
            "Assembled Height (in.)": "19.7",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "66743"
    assert normalized["categoryName"] == "Play Table & Chair Sets"
    assert normalized["aspects"]["Type"] == ["Play Table & Chair Set"]
    assert normalized["aspects"]["Set Includes"] == ["Table & Chairs"]
    assert normalized["aspects"]["Room"] == ["Playroom"]


def test_quality_gate_routes_bed_storage_titles_out_of_cabinet_category():
    bunk_bed = normalize_generated_listing(
        {
            "title": "Queen Over Queen Bunk Bed with Storage Cabinets and USB Ports",
            "description": "<div>Heavy-duty bunk bed with underbed storage and guardrails.</div>",
            "categoryId": "20487",
            "categoryName": "Cabinets & Cupboards",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Queen Over Queen Bunk Bed with Storage Cabinets and USB Ports",
        attributes={
            "Assembled Length (in.)": "83.1",
            "Assembled Width (in.)": "63.5",
            "Assembled Height (in.)": "65.2",
        },
        specs={},
        images=["img1", "img2"],
    )
    loft_bed = normalize_generated_listing(
        {
            "title": "Full Size Metal Loft Bed with Stairs Wardrobe Storage Shelves LED Light",
            "description": "<div>Modern loft bed frame with integrated wardrobe and staircase.</div>",
            "categoryId": "20487",
            "categoryName": "Cabinets & Cupboards",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Full Size Metal Loft Bed with Stairs Wardrobe Storage Shelves LED Light",
        attributes={
            "Assembled Length (in.)": "93.3",
            "Assembled Width (in.)": "57.2",
            "Assembled Height (in.)": "70.9",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert bunk_bed["categoryId"] == "175758"
    assert bunk_bed["categoryName"] == "Beds & Bed Frames"
    assert bunk_bed["aspects"]["Type"] == ["Bunk Bed"]
    assert loft_bed["categoryId"] == "175758"
    assert loft_bed["categoryName"] == "Beds & Bed Frames"
    assert loft_bed["aspects"]["Type"] == ["Loft Bed"]


def test_quality_gate_normalizes_pantry_titles_and_specifics():
    normalized = normalize_generated_listing(
        {
            "title": "[Assembly Video Provided] Modern 83.7in High Kitchen Pantry, Freestanding Tall Hutch with Faux Marble Top, 5 Drawers, Adjustable Shelf, Gray",
            "description": "<div>Kitchen pantry cabinet with storage shelves and faux marble top.</div>",
            "categoryId": "38217",
            "categoryName": "China Cabinets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="[Assembly Video Provided] Modern 83.7in High Kitchen Pantry, Freestanding Tall Hutch with Faux Marble Top, 5 Drawers, Adjustable Shelf, Gray",
        attributes={
            "Assembled Length (in.)": "15.7",
            "Assembled Width (in.)": "41.7",
            "Assembled Height (in.)": "83.7",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["title"].startswith("Modern 83.7in High Kitchen Pantry")
    assert "[Assembly Video Provided]" not in normalized["title"]
    assert normalized["categoryId"] == "20487"
    assert normalized["categoryName"] == "Cabinets & Cupboards"
    assert normalized["aspects"]["Type"] == ["Cabinet"]


def test_quality_gate_normalizes_treadmill_from_motors_category():
    normalized = normalize_generated_listing(
        {
            "title": "Electric Treadmill with Auto Incline Foldable 3.5HP Running Machine 61x28x48in Black",
            "description": "<div>Foldable home treadmill with incline and LCD display.</div>",
            "categoryId": "262210",
            "categoryName": "Running Boards & Nerf Bars",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Electric Treadmill with Auto Incline Foldable 3.5HP Running Machine 61x28x48in Black",
        attributes={
            "Assembled Length (in.)": "61",
            "Assembled Width (in.)": "28",
            "Assembled Height (in.)": "48",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "15280"
    assert normalized["categoryName"] == "Treadmills"
    assert normalized["aspects"]["Type"] == ["Treadmill"]


def test_quality_gate_routes_potting_bench_to_greenhouses():
    normalized = normalize_generated_listing(
        {
            "title": "Tall Garden Potting Bench Table with Hutch 65.7 in H Brown Wood Rustic",
            "description": "<div>Outdoor potting bench workstation with hutch shelves and storage.</div>",
            "categoryId": "38217",
            "categoryName": "China Cabinets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Tall Garden Potting Bench Table with Hutch 65.7 in H Brown Wood Rustic",
        attributes={
            "Assembled Length (in.)": "50.0",
            "Assembled Width (in.)": "20.3",
            "Assembled Height (in.)": "65.7",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["categoryId"] == "139939"
    assert normalized["categoryName"] == "Greenhouses"
    assert normalized["aspects"]["Type"] == ["Potting Bench"]


def test_quality_gate_sets_dining_set_item_count_from_title():
    normalized = normalize_generated_listing(
        {
            "title": "5-Piece Outdoor Acacia Wood Round Dining Set Solid Wood Patio Table with 4 Barrel Chairs",
            "description": "<div>Round patio dining set with 4 chairs and one table.</div>",
            "categoryId": "107578",
            "categoryName": "Dining Sets",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="5-Piece Outdoor Acacia Wood Round Dining Set Solid Wood Patio Table with 4 Barrel Chairs",
        attributes={
            "Assembled Length (in.)": "44.5",
            "Assembled Width (in.)": "44.5",
            "Assembled Height (in.)": "29.5",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["aspects"]["Number of Items in Set"] == ["5"]
    assert normalized["aspects"]["Number of Pieces"] == ["5"]


def test_quality_gate_blocks_unsupported_foldable_claim_from_ai_draft():
    normalized = normalize_generated_listing(
        {
            "title": "Modern Accent Chair Foldable Linen Lounge Chair",
            "description": "<div>Foldable living room chair for compact storage.</div>",
            "categoryId": "54235",
            "categoryName": "Chairs",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Modern Accent Chair Linen Lounge Chair",
        source_description="<div>Accent chair for living room seating.</div>",
        attributes={
            "Assembled Length (in.)": "32",
            "Assembled Width (in.)": "30",
            "Assembled Height (in.)": "34",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["source_facts"]["claims"]["foldable"]["supported"] is False

    issues = validate_listing_quality(
        normalized,
        source_title="Modern Accent Chair Linen Lounge Chair",
        source_description="<div>Accent chair for living room seating.</div>",
        attributes={
            "Assembled Length (in.)": "32",
            "Assembled Width (in.)": "30",
            "Assembled Height (in.)": "34",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert any(issue.code == "unsupported_foldable_claim" for issue in issues)


def test_quality_gate_blocks_unsupported_charging_claim_from_ai_draft():
    normalized = normalize_generated_listing(
        {
            "title": "Modern Style L-Shaped Sectional Sofa with USB Ports",
            "description": "<div>Sectional sofa with hidden USB charging for daily use.</div>",
            "categoryId": "38208",
            "categoryName": "Sofas, Armchairs & Couches",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Modern Style L-Shaped Sectional Sofa 102 in Black",
        source_description="<div>Sectional sofa with chaise lounge and storage seat.</div>",
        attributes={
            "Assembled Length (in.)": "102",
            "Assembled Width (in.)": "61",
            "Assembled Height (in.)": "35",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert normalized["source_facts"]["claims"]["charging"]["supported"] is False

    issues = validate_listing_quality(
        normalized,
        source_title="Modern Style L-Shaped Sectional Sofa 102 in Black",
        source_description="<div>Sectional sofa with chaise lounge and storage seat.</div>",
        attributes={
            "Assembled Length (in.)": "102",
            "Assembled Width (in.)": "61",
            "Assembled Height (in.)": "35",
        },
        specs={},
        images=["img1", "img2"],
    )

    assert any(issue.code == "unsupported_charging_claim" for issue in issues)


def test_quality_gate_blocks_non_publishable_source_video_manifest():
    normalized = normalize_generated_listing(
        {
            "title": "Large Wooden Chicken Coop Outdoor Hen House",
            "description": "<div>Outdoor chicken coop with nesting box and run.</div>",
            "categoryId": "63108",
            "categoryName": "Cages, Hutches & Enclosures",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Large Wooden Chicken Coop Outdoor Hen House",
        source_description="<div>Outdoor chicken coop with nesting box and run.</div>",
        attributes={
            "Assembled Length (in.)": "120",
            "Assembled Width (in.)": "27",
            "Assembled Height (in.)": "42",
        },
        specs={},
        images=["img1", "img2"],
        videos=["https://media.example.com/video_links.txt"],
    )

    assert normalized["source_facts"]["video"]["preflight_status"] == "blocked"
    assert "source_video_txt_manifest" in normalized["source_facts"]["video"]["issue_codes"]

    issues = validate_listing_quality(
        normalized,
        source_title="Large Wooden Chicken Coop Outdoor Hen House",
        source_description="<div>Outdoor chicken coop with nesting box and run.</div>",
        attributes={
            "Assembled Length (in.)": "120",
            "Assembled Width (in.)": "27",
            "Assembled Height (in.)": "42",
        },
        specs={},
        images=["img1", "img2"],
        videos=["https://media.example.com/video_links.txt"],
    )

    assert any(issue.code == "source_video_not_publishable" for issue in issues)


def test_generation_and_publish_entrypoints_call_quality_gate():
    for relative_path in ("batch_analyze.py", "daily_tasks.py", "server.py", "batch_publish.py"):
        source = (ROOT / relative_path).read_text(encoding="utf-8-sig")

        assert "normalize_generated_listing" in source or "_normalize_generated_listing" in source
        assert "validate_listing_quality" in source or "_validate_listing_quality" in source
        assert "blocking_issue_messages" in source or "_quality_blocking_messages" in source


def test_analysis_entrypoints_pass_shared_category_matcher():
    assert "category_matcher=CATEGORY_MATCHER" in (ROOT / "batch_analyze.py").read_text(encoding="utf-8-sig")
    assert "category_matcher=CATEGORY_MATCHER" in (ROOT / "daily_tasks.py").read_text(encoding="utf-8-sig")
    assert "category_matcher=_get_quality_gate_category_matcher()" in (ROOT / "server.py").read_text(encoding="utf-8-sig")
