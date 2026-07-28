"""
eBay Category & Item Specifics Auto-Matcher Service

Automatically:
1. Gets the correct category ID based on product title using eBay Taxonomy API
2. Fetches required/recommended Item Specifics for that category
3. Auto-fills missing required Item Specifics based on existing data
"""
import html
import re
import requests
import logging
from typing import Dict, List, Optional, Any, Tuple
from src.services.ebay_auth import EbayOAuthService
from src.utils.store_profile import get_store_profile


class EbayCategoryMatcher:
    """Automatic category and item specifics matching"""
    
    def __init__(self, oauth_service: EbayOAuthService):
        self.oauth = oauth_service
        self.base_url = "https://api.ebay.com"
        self._aspects_cache: Dict[str, List[Dict]] = {}  # Cache category aspects

    def canonicalize_category(
        self,
        title: str,
        category_id: str,
        category_name: str = None,
        description: str = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Remap stale or semantically drifted category IDs to current tree-0 leaves."""
        cid = str(category_id or "").strip()
        if not cid:
            return None, category_name

        text_lower = self._normalize_keyword_text(
            " ".join(part for part in (title or "", description or "", category_name or "") if part)
        )
        title_lower = self._normalize_keyword_text(title or "")
        has_plant_stand = any(
            kw in text_lower
            for kw in (
                "plant stand",
                "plant pedestal",
                "garden pedestal",
                "roman column",
                "column pedestal",
                "pillar",
            )
        )
        has_planter = any(kw in text_lower for kw in ("planter", "flower pot", "plant pot", "window box"))
        has_statuary = any(kw in text_lower for kw in ("statue", "sculpture", "figurine", "ornament"))
        has_decor_lighting = any(kw in text_lower for kw in ("solar", "lantern"))
        has_fountain = any(kw in text_lower for kw in ("fountain", "water feature", "waterfall fountain"))
        has_frog_statuary = any(
            kw in text_lower
            for kw in ("frog garden stool", "garden frog", "frog statue", "frog sitting on rock")
        )
        # "Sofa Table" / "Sofa Side Table" / "Console Table ... Behind Couch"
        # are tables that mention a sofa — never treat them as seating
        # (2026-07-13 live incident: nightstand + console table remapped to 38208).
        has_sofa_accessory_table = (
            re.search(r"\b(?:sofa|couch)\s+(?:side\s+)?table\b", title_lower) is not None
            or "console table" in title_lower
            or re.search(r"\bbehind\s+(?:the\s+)?(?:couch|sofa)\b", title_lower) is not None
        )
        has_sofa = not has_sofa_accessory_table and (
            any(
                kw in title_lower
                for kw in (
                    "sectional sofa",
                    "l-shaped sectional",
                    "l-shaped sofa",
                    "sleeper sofa",
                    "sofa bed",
                    "pull out sofa",
                    "loveseat",
                )
            )
            or re.search(r"\b(?:sofa|couch)\b", title_lower) is not None
        )
        has_bunk_bed = any(
            kw in title_lower
            for kw in (
                "bunk bed",
                "loft bed",
                "queen over queen",
                "twin over twin",
                "twin over full",
                "full over full",
                "full over queen",
                "queen over full",
            )
        )
        # Word-boundary match: plain substring turned "Storage Bedside Table"
        # into "storage bed" and remapped a nightstand to Bed Frames (175758).
        has_bed_frame = any(
            re.search(rf"\b{kw}\b", title_lower)
            for kw in ("platform bed", "bed frame", "house bed", "floor bed", "montessori bed", "storage bed")
        )
        has_storage_ottoman = any(
            kw in text_lower
            for kw in ("storage ottoman", "ottoman bench", "lift top ottoman", "storage footstool")
        )
        has_storage_ottoman_only = has_storage_ottoman and not any(
            kw in title_lower
            for kw in (
                "sectional sofa",
                "l-shaped sectional",
                "l-shaped sofa",
                "sleeper sofa",
                "sofa bed",
                "pull out sofa",
                "loveseat",
                "sofa",
                "couch",
                "armchair",
                "accent chair",
                "reading chair",
                "club chair",
                "chaise lounge",
            )
        )
        has_bean_bag = "bean bag" in title_lower
        has_bar_stool = any(
            kw in title_lower
            for kw in (
                "bar stool",
                "bar stools",
                "counter stool",
                "counter stools",
                "barstool",
                "barstools",
            )
        )
        has_desk = any(
            kw in title_lower
            for kw in (
                "computer desk",
                "writing desk",
                "executive desk",
                "l-shaped desk",
                "l shaped desk",
                "corner desk",
                "standing desk",
                "home office desk",
            )
        ) and not (has_bunk_bed or has_bed_frame)
        has_treadmill = (
            any(kw in title_lower for kw in ("treadmill", "walking pad", "running machine"))
            and not any(kw in title_lower for kw in ("dog treadmill", "pet treadmill"))
        )
        has_potting_bench = any(kw in text_lower for kw in ("potting bench", "garden workstation"))
        has_outdoor_context = any(
            kw in text_lower
            for kw in ("outdoor", "patio", "garden", "backyard", "poolside", "deck", "balcony", "porch")
        )
        has_porch_swing = (
            "porch swing" in text_lower
            or "porch swing bed" in text_lower
            or "patio swing bed" in text_lower
            or "garden swing bed" in text_lower
            or (
                "swing bed" in text_lower
                and has_outdoor_context
                and "rope" in text_lower
                and not any(kw in text_lower for kw in ("cat ", "dog ", "pet ", "kitten", "puppy"))
            )
        )
        # An indoor product that merely mentions one outdoor placement is not an
        # outdoor product. "65.75\" Upholstered Storage Bench ... Bench Daybed ...
        # For Living Room, Entryway, Dormitory, Bedroom" was moved to Outdoor
        # Daybeds on live because the copy said "or a leisure bench on the
        # balcony" — one incidental word outweighed four named indoor rooms
        # (2026-07-27). Require the outdoor signal in the TITLE whenever the copy
        # explicitly places the item indoors.
        names_indoor_room = any(
            kw in text_lower
            for kw in (
                "living room", "bedroom", "entryway", "dormitory", "dorm room",
                "study", "home office", "hallway", "foyer", "nursery",
            )
        )
        outdoor_in_title = any(
            kw in title_lower
            for kw in ("outdoor", "patio", "garden", "backyard", "poolside", "deck", "balcony", "porch")
        )
        daybed_outdoor_signal = outdoor_in_title if names_indoor_room else has_outdoor_context

        has_outdoor_daybed = any(kw in title_lower for kw in ("outdoor daybed", "patio daybed", "sunbed")) or (
            "daybed" in title_lower
            and daybed_outdoor_signal
            and not has_porch_swing
            and not any(kw in title_lower for kw in ("sofa", "couch", "loveseat", "sectional"))
        )
        has_patio_furniture_set = any(
            kw in text_lower
            for kw in (
                "patio furniture set",
                "patio set",
                "patio conversation",
                "outdoor conversation set",
                "outdoor furniture set",
                "outdoor sectional",
            )
        )
        has_outdoor_chair = (
            has_outdoor_context
            and not has_outdoor_daybed
            and not any(kw in title_lower for kw in ("sofa", "couch", "loveseat", "sectional"))
            and re.search(r"\btable\b", title_lower) is None
            and any(
                kw in title_lower
                for kw in (
                    "outdoor chair",
                    "patio chair",
                    "club chair",
                    "club chairs",
                    "armchair",
                    "armchairs",
                    "patio armchair",
                    "patio armchairs",
                    "outdoor dining chair",
                    "outdoor dining chairs",
                    "patio dining chair",
                    "patio dining chairs",
                    "outdoor lounge chair",
                    "outdoor lounge chairs",
                    "patio lounge",
                    "sun lounger",
                    "camping chair",
                    "camping chairs",
                )
            )
        )
        has_putting_green = "golf" in title_lower and (
            "golf putting green" in title_lower or "putting green" in title_lower
        )
        has_golf_mat = (
            "golf" in title_lower
            and not has_putting_green
            and any(
                kw in title_lower
                for kw in ("golf hitting mat", "golf practice mat", "golf training mat", "golf swing mat")
            )
        )
        has_game_table = any(kw in title_lower for kw in ("board game table", "gaming table", "game table"))
        has_shade_sail = any(kw in title_lower for kw in ("shade sail", "sun shade sail", "sunshade sail"))
        has_dining_set = not has_bar_stool and not has_patio_furniture_set and (
            any(
                kw in title_lower
                for kw in (
                    "dining set",
                    "dining table set",
                    "piece dining set",
                    "5-piece dining",
                    "5 piece dining",
                    "6-piece dining",
                    "6 piece dining",
                )
            )
            or any(
                kw in text_lower
                for kw in (
                    "dining table and chair",
                    "dining table with chair",
                    "table and chair set",
                    "table with chair set",
                )
            )
        )
        pantry_markers = (
            "kitchen pantry",
            "pantry cabinet",
            "freestanding pantry",
            "pantry storage",
            "storage cabinet",
            "cupboard",
            "kitchen hutch",
            "hutch cabinet",
            "microwave shelf",
        )
        has_pantry_cabinet = not (has_bunk_bed or has_bed_frame) and (
            any(kw in text_lower for kw in pantry_markers)
            or ("hutch" in text_lower and any(kw in text_lower for kw in ("kitchen", "pantry", "microwave")))
        )
        has_display_cabinet = (
            not has_pantry_cabinet
            and any(kw in text_lower for kw in ("display cabinet", "curio cabinet", "glass cabinet"))
        )
        # "cooler" is also the comparative of "cool", and outdoor copy is full of
        # it ("cozy even in cooler weather", "ideal for cooler months"). The bare
        # \bcooler\b match therefore wanted to move 4 bell tents into
        # "Ice Chests & Coolers" — and category_mismatch carries a categoryId fix
        # key, so a --fix run would have done it on live (2026-07-27).
        # Only count "cooler" when it is not modifying a temperature/time noun.
        has_cooler = any(
            kw in text_lower
            for kw in ("hard cooler", "insulated cooler", "ice chest", "portable cooler", "cooler can")
        ) or (
            re.search(r"\bcooler\b", text_lower) is not None
            and re.search(
                r"\bcooler\s+(?:weather|temperatures?|months?|days?|nights?|evenings?|"
                r"seasons?|climates?|air|conditions?|environments?|areas?|regions?)\b",
                text_lower,
            )
            is None
        )
        excluded_bench_context = (
            has_storage_ottoman
            or any(
                kw in text_lower
                for kw in (
                    "garden bench",
                    "outdoor bench",
                    "park bench",
                    "shower bench",
                    "bath stool",
                    "potting bench",
                    "hall tree",
                    "coat rack bench",
                    "shoe bench",
                )
            )
        )
        has_indoor_bench = not excluded_bench_context and any(
            kw in text_lower
            for kw in (
                "dining bench",
                "long bench",
                "upholstered bench",
                "mid century bench",
                "bench with",
                "end of bed",
                "footrest stool",
            )
        )
        indoor_table_markers = (
            "coffee table",
            "cocktail table",
            "nesting table",
            "side table",
            "end table",
            "console table",
            "center table",
        )
        has_kids_table_set = not any(kw in title_lower for kw in indoor_table_markers) and any(
            kw in text_lower
            for kw in (
                "kids activity table",
                "kids table",
                "kids table and chair",
                "kids table & chair",
                "children table and chair",
                "children's table and chair",
                "childrens table and chair",
                "play table and chair",
                "play table & chair",
                "activity table and chair",
                "toddler table and chair",
                "table and chair set for toddlers",
            )
        )
        has_outdoor_table = any(
            kw in text_lower
            for kw in ("patio dining table", "outdoor dining table", "bistro table", "garden table", "outdoor table", "patio table")
        )
        has_wine_storage = (
            any(kw in text_lower for kw in ("wine rack", "wine cabinet", "stemware rack"))
            or (
                "bar cabinet" in text_lower
                and "coffee bar cabinet" not in text_lower
                and any(kw in text_lower for kw in ("wine", "stemware", "liquor", "bottle"))
            )
        )
        has_explicit_table = any(
            kw in text_lower
            for kw in (
                "coffee table",
                "cocktail table",
                "dining table",
                "farm table",
                "pub table",
                "bar table",
                "console table",
                "end table",
                "side table",
                "nesting table",
                "board game table",
                "gaming table",
                "game table",
            )
        )

        direct_replacements = {
            "116363": ("100411", "Litter Boxes"),
            "20751": ("20744", "Beds"),
            "20743": ("20744", "Beds"),
            "116365": ("20748", "Fences & Exercise Pens"),
            "121856": ("20748", "Fences & Exercise Pens"),
            "116366": ("108884", "Dog Houses"),
            "116403": ("116389", "Ramps & Stairs"),
            "38205": ("38204", "Tables"),
            "42428": ("20487", "Cabinets & Cupboards"),
            "45331": ("20689", "Wine Racks & Bottle Holders"),
            "66756": ("66743", "Play Table & Chair Sets"),
            "175761": ("20490", "Ottomans, Footstools & Poufs"),
        }
        if cid in direct_replacements:
            return direct_replacements[cid]

        if has_bunk_bed and cid != "175758":
            return "175758", "Beds & Bed Frames"

        if has_bed_frame and not has_bunk_bed and cid != "175758":
            return "175758", "Beds & Bed Frames"

        if has_bar_stool and cid != "103431":
            return "103431", "Bar Stools & Stools"

        if has_desk and cid != "88057":
            return "88057", "Desks & Tables"

        if has_treadmill and cid != "15280":
            return "15280", "Treadmills"

        if has_potting_bench and cid != "139939":
            return "139939", "Greenhouses"

        if has_putting_green and cid != "36234":
            return "36234", "Putting Greens & Aids"

        if has_golf_mat and cid != "50876":
            return "50876", "Nets, Cages & Mats"

        if has_porch_swing and cid != "79694":
            return "79694", "Porch Swings"

        if has_outdoor_daybed and cid != "138996":
            return "138996", "Outdoor Daybeds"

        if has_outdoor_chair and cid != "79684":
            return "79684", "Outdoor Chairs"

        if has_shade_sail and cid != "180997":
            return "180997", "Shade Sails"

        if has_game_table and cid != "38204":
            return "38204", "Tables"

        if has_dining_set and cid != "107578":
            return "107578", "Dining Sets"

        if has_pantry_cabinet and cid != "20487":
            return "20487", "Cabinets & Cupboards"

        if has_display_cabinet and cid != "20493":
            return "20493", "Display Cabinets"

        if has_patio_furniture_set and not has_outdoor_chair and not has_dining_set and cid != "139849":
            return "139849", "Patio & Garden Furniture Sets"

        if has_cooler and cid != "79691":
            return "79691", "Ice Chests & Coolers"

        if has_bean_bag and cid != "48319":
            return "48319", "Bean Bags & Inflatables"

        if has_explicit_table and not has_sofa and not has_patio_furniture_set and cid == "38208":
            return "38204", "Tables"

        if has_sofa and not has_bean_bag and not has_patio_furniture_set and not (has_bunk_bed or has_bed_frame) and cid != "38208":
            return "38208", "Sofas, Armchairs & Couches"

        if has_storage_ottoman_only and cid != "20490":
            return "20490", "Ottomans, Footstools & Poufs"

        if has_indoor_bench and not has_dining_set and cid != "262980":
            return "262980", "Benches"

        if has_kids_table_set and cid != "66743":
            return "66743", "Play Table & Chair Sets"

        if (
            has_outdoor_table
            and not has_dining_set
            and cid in {"38204", "79686", "107578"}
            and not any(kw in title_lower for kw in indoor_table_markers)
        ):
            return "112590", "Patio & Garden Tables"

        if has_wine_storage and cid == "45331":
            return "20689", "Wine Racks & Bottle Holders"

        if has_fountain and cid in {"20740", "20518", "29511", "29514", "38208"}:
            return "20507", "Outdoor Fountains"

        if has_frog_statuary and cid in {"20740", "20518", "29514", "79682", "79684", "38208"}:
            return "29511", "Ornaments & Statues"

        if cid == "75669" and any(kw in text_lower for kw in ("garden cart", "dump cart", "wagon", "wheelbarrow", "tow behind")):
            return "75671", "Wheelbarrows, Carts & Wagons"

        if cid == "38204":
            if has_patio_furniture_set:
                return "139849", "Patio & Garden Furniture Sets"
            if has_indoor_bench:
                return "262980", "Benches"
            if has_explicit_table:
                return cid, category_name
            if any(kw in text_lower for kw in ("dump cart", "wagon", "wheelbarrow", "tow behind", "hauling", "cart trailer")):
                return "75671", "Wheelbarrows, Carts & Wagons"
            if has_plant_stand:
                return "29514", "Plant Stands"
            if has_decor_lighting:
                return "29511", "Ornaments & Statues"
            if has_statuary and has_planter:
                return "20518", "Baskets, Pots, Window Boxes & Saucers"
            if has_statuary:
                return "29511", "Ornaments & Statues"
            if has_planter:
                return "20518", "Baskets, Pots, Window Boxes & Saucers"

        if cid == "20744" and any(kw in text_lower for kw in ("cat tree", "cat tower", "cat condo", "scratching post", "cat scratch", "catio")):
            return "20740", "Furniture & Scratchers"

        if cid == "116364":
            if any(kw in text_lower for kw in ("cat swing bed", "cat hammock", "hanging cat bed", "hammock", "swing bed", "nester")):
                return "149074", "Beds, Hammocks & Nesters"
            return "66762", "Beds"

        if cid == "116371":
            if any(kw in text_lower for kw in ("litter", "enclosure", "litter box")):
                return "100411", "Litter Boxes"
            return "20740", "Furniture & Scratchers"

        if cid == "116379":
            if "stroller" in text_lower:
                return "116380", "Strollers"
            return "177788", "Carriers & Totes"

        if cid == "116391":
            if any(kw in text_lower for kw in ("litter", "enclosure", "litter box", "cat litter")):
                return "100411", "Litter Boxes"
            return "20740", "Furniture & Scratchers"

        if cid == "116394" and any(kw in text_lower for kw in ("chicken", "poultry", "hen house", "chicken run", "chicken coop")):
            return "63108", "Cages, Hutches & Enclosure"

        if cid in {"20497", "34386"}:
            if has_plant_stand:
                return "29514", "Plant Stands"

            if has_decor_lighting:
                return "29511", "Ornaments & Statues"
            if has_statuary and has_planter:
                return "20518", "Baskets, Pots, Window Boxes & Saucers"
            if has_statuary:
                return "29511", "Ornaments & Statues"
            if has_planter:
                return "20518", "Baskets, Pots, Window Boxes & Saucers"

        return cid, category_name

    def get_keyword_category_hint(
        self,
        title: str,
        description: str = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return the local keyword-only category hint without Taxonomy API calls."""
        category_id, category_name = self._fallback_category(title, description)
        return self.canonicalize_category(title, category_id, category_name, description)
        
    def get_category_and_aspects(self, title: str, existing_aspects: Dict[str, List[str]] = None, description: str = None) -> Tuple[str, str, Dict[str, List[str]]]:
        """
        Main method: Get category ID and complete aspects for a product
        
        Args:
            title: Product title
            existing_aspects: Already collected aspects (from AI/scraping)
            description: Product description (used as fallback for category matching)
            
        Returns:
            Tuple of (category_id, category_name, completed_aspects)
        """
        existing_aspects = existing_aspects or {}
        
        # Step 1: Get category suggestion (use both title and description)
        category_id, category_name = self._get_best_category(title, description)
        
        if not category_id:
            logging.warning(f"[WARN] No category found for: {title[:50]}")
            return None, None, existing_aspects
        
        logging.info(f"[OK] Category: {category_id} ({category_name})")
        
        # Step 2: Get required/recommended aspects for this category
        required_aspects, recommended_aspects = self._get_category_aspects(category_id)
        
        # Step 3: Auto-fill missing required aspects
        completed_aspects = self._complete_aspects(
            existing_aspects, 
            required_aspects, 
            recommended_aspects,
            title,
            category_name
        )
        
        return category_id, category_name, completed_aspects
    
    def _get_best_category(self, title: str, description: str = None) -> Tuple[Optional[str], Optional[str]]:
        """Get the best category ID for a product title.
        
        Strategy: Get keyword-based match first. If it finds a strong match,
        use it directly (our keyword rules are more reliable for our specific
        product types). Only fall back to eBay API when keywords don't match.
        When API is used, cross-validate against keyword match to catch
        API mis-categorizations (e.g., outdoor chaise → sofas).
        """
        # Step 1: Try keyword-based match first (most reliable for known types)
        kw_cat_id, kw_cat_name = self._fallback_category(title, description)
        kw_cat_id, kw_cat_name = self.canonicalize_category(title, kw_cat_id, kw_cat_name, description)
        
        # Step 2: Try eBay Taxonomy API
        api_cat_id, api_cat_name = None, None
        try:
            token = self.oauth.get_application_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            }
            # Auto parts only exist in the Motors tree (100); EBAY_US uses 0.
            url = (
                f"{self.base_url}/commerce/taxonomy/v1/category_tree/"
                f"{get_store_profile().category_tree_id}/get_category_suggestions"
            )
            params = {"q": title[:100]}
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                suggestions = data.get("categorySuggestions", [])
                if suggestions:
                    best = suggestions[0].get("category", {})
                    api_cat_id = best.get("categoryId")
                    api_cat_name = best.get("categoryName")
                    api_cat_id, api_cat_name = self.canonicalize_category(title, api_cat_id, api_cat_name, description)
        except Exception as e:
            logging.warning(f"[WARN] Taxonomy API failed: {e}")
        
        # Step 3: Cross-validate and decide
        if kw_cat_id and kw_cat_id != "38208":  # Not the generic fallback default
            # Strong keyword match — trust it over API
            if api_cat_id and api_cat_id != kw_cat_id:
                logging.info(f"[CAT] Keyword={kw_cat_id} ({kw_cat_name}), API={api_cat_id} ({api_cat_name}) — using keyword match")
            return kw_cat_id, kw_cat_name
        
        if api_cat_id:
            # Verify API result isn't a known bad mapping
            if self._is_api_category_plausible(title, api_cat_id, api_cat_name):
                return api_cat_id, api_cat_name
            else:
                logging.warning(f"[CAT] API suggested {api_cat_id} ({api_cat_name}) rejected as implausible for: {title[:60]}")
                return kw_cat_id, kw_cat_name
        
        return kw_cat_id, kw_cat_name

    def is_category_plausible_for_text(
        self,
        title: str,
        category_id: str,
        category_name: str = None,
    ) -> bool:
        """Check whether a category is semantically plausible for a product title."""
        title_lower = self._normalize_keyword_text(title)
        cid, category_name = self.canonicalize_category(title, category_id, category_name)
        cid = str(cid or "").strip()
        if not cid:
            return False

        tent_keywords = [
            "camping tent", "camping tents", "inflatable tent", "inflatable tents",
            "inflatabletent", "glamping tent", "glamping tents", "blow up tent",
            "blow-up tent", "air tent", "suv tent", "bell tent", "yurt tent",
        ]
        grill_keywords = [
            "bbq grill", "barbecue grill", "propane grill", "gas grill",
            "grill with side burner", "grill & griddle", "grill griddle",
        ]
        pet_categories = {
            "100411", "108884", "116362", "116363", "116380", "116383", "116388", "116389",
            "117029", "121851", "149074", "177788", "20740", "20744", "20748", "46289",
            "63108", "66762", "77639", "63112", "26684", "14769",
        }
        decor_categories = {"20518", "29511", "36025", "86916", "260926"}
        plant_stand_categories = {"29514"}
        motors_keywords = [
            "running board", "running boards", "nerf bar", "nerf bars",
            "side step", "side steps", "step bar", "truck step",
            "roof rack", "cross bar", "crossbars", "cargo carrier",
            "trailer hitch", "tailgate assist", "tailgate ladder", "tailgate handle", "silverado", "sierra", "wrangler",
            "f-150", "f150", "tacoma", "ram 1500", "glc", "glb", "pickup",
        ]
        # eBay Motors ids plus tree-0 equivalents. An account without eBay Motors
        # selling privileges is refused those categories at publish (errorId 25005)
        # even though they are correct, so a rack/carrier may legitimately be
        # listed under 177849 "Car & Truck Racks", which lives in the normal
        # EBAY_US tree. Additive — the Motors ids stay valid for accounts that
        # do have the privilege.
        motors_categories = {
            "262210", "174020", "174021", "262216", "262093", "85040",
            "177849",  # Car & Truck Racks (tree 0)
        }
        outdoor_chair_categories = {"79682", "79684", "138996"}
        porch_swing_categories = {"79694"}
        sofa_categories = {"38208"}
        ottoman_categories = {"20490"}
        bean_bag_categories = {"48319"}
        trellis_categories = {"43538"}
        arbor_categories = {"180993"}
        luggage_categories = {"16080"}
        arcade_game_categories = {"36278", "20270", "13716", "2540"}
        kids_table_set_categories = {"66743"}
        outdoor_table_categories = {"112590"}
        wine_storage_categories = {"20689"}
        hall_tree_categories = {"261263"}
        patio_furniture_set_categories = {"139849"}
        dining_set_categories = {"107578"}
        cooler_categories = {"79691"}
        indoor_bench_categories = {"262980"}
        desk_categories = {"88057", "25290"}
        table_categories = {"38204", "107578"}
        game_table_categories = {"38204"}
        table_tennis_categories = {"97075", "97076", "158955"}
        shade_sail_categories = {"180997"}
        sandbox_categories = {"145990"}
        cart_categories = {"75671", "95495"}
        putting_green_categories = {"36234"}
        golf_mat_categories = {"50876"}
        golf_product_markers = (
            "golf bag", "golf club", "golf clubs", "golf practice", "golf training",
            "hitting net", "batting cage", "golf simulator", "club holder", "club rack",
        )
        outdoor_shelter_markers = (
            "gazebo", "pergola", "outdoor canopy", "patio canopy", "garden canopy",
        )
        porch_swing_markers = (
            "porch swing",
            "porch swing bed",
            "patio swing bed",
            "garden swing bed",
        )
        trellis_markers = (
            "garden trellis",
            "metal trellis",
            "plant trellis",
            "trellis for climbing",
            "climbing plants support",
        )
        arbor_markers = (
            "garden arch",
            "rose arch",
            "garden arbor",
            "arbor arch",
            "arch with gate",
        )
        shade_sail_markers = ("shade sail", "sun shade sail", "sunshade sail")
        game_table_markers = ("board game table", "gaming table", "game table")
        table_tennis_markers = ("table tennis", "ping pong")
        golf_putting_green_markers = ("golf putting green", "putting green")
        golf_hitting_mat_markers = (
            "golf hitting mat",
            "golf practice mat",
            "golf training mat",
            "golf swing mat",
        )
        indoor_recliner_markers = (
            "recliner", "recliner chair", "manual reclining", "reclining footrest",
            "armchair", "accent chair",
        )
        luggage_markers = (
            "spinner wheel luggage",
            "spinner luggage",
            "hard shell luggage",
            "luggage set",
            "carry on suitcase",
            "travel suitcase",
            "checked luggage",
            "travel luggage",
        )
        basketball_arcade_markers = (
            "basketball arcade",
            "arcade basketball",
            "dual shot",
            "electronic scorer",
        )

        pet_identity_terms = ("cat ", " cat", "cats", "dog ", " dog", "dogs", "pet ", " pet", "kitten", "puppy")
        pet_product_markers = (
            "cat tree", "cat tower", "cat condo", "cat bed", "cat cabinet", "cat house", "cat litter",
            "dog crate", "dog kennel", "dog cage", "dog bed", "dog house", "dog ramp", "dog stairs",
            "pet gate", "pet fence", "pet carrier", "pet stroller", "pet playpen", "dog treadmill",
            "pet treadmill", "scratching post", "cat scratch", "hidden design", "cat retreat",
            "chicken coop", "chicken house", "poultry coop", "hen house", "chicken run",
            "rabbit hutch", "rabbit cage",
        )
        decor_markers = (
            "planter", "statue", "figurine", "sculpture", "ornament", "solar lantern",
            "garden decor", "yard decor", "flower bed",
        )
        statuary_markers = (
            "statue", "figurine", "sculpture", "ornament", "solar lantern", "garden decor",
        )
        fountain_markers = ("fountain", "water feature", "waterfall fountain", "cascading water")
        garden_stool_markers = ("garden stool", "outdoor stool", "patio stool")
        frog_statuary_markers = ("frog garden stool", "garden frog", "frog statue", "frog sitting on rock")
        sofa_markers = (
            "sectional sofa", "l-shaped sectional", "l-shaped sofa", "sleeper sofa",
            "sofa bed", "pull out sofa", "loveseat", "couch",
        )
        bunk_bed_markers = (
            "bunk bed",
            "loft bed",
            "queen over queen",
            "twin over twin",
            "twin over full",
            "full over full",
            "full over queen",
            "queen over full",
        )
        bed_frame_markers = ("platform bed", "bed frame", "house bed", "floor bed", "montessori bed", "storage bed")
        ottoman_markers = ("storage ottoman", "ottoman bench", "lift top ottoman", "storage footstool")
        bean_bag_markers = ("bean bag",)
        patio_furniture_set_markers = (
            "patio furniture set",
            "patio set",
            "patio conversation",
            "outdoor conversation set",
            "outdoor furniture set",
            "outdoor sectional",
        )
        dining_set_markers = (
            "dining set",
            "dining table set",
            "piece dining set",
            "5-piece dining",
            "5 piece dining",
            "6-piece dining",
            "6 piece dining",
            "table set",
            "kitchen table set",
            "dining table and chair",
            "dining table with chair",
            "table and chair set",
            "table with chair set",
        )
        bar_stool_markers = (
            "bar stool",
            "bar stools",
            "counter stool",
            "counter stools",
            "barstool",
            "barstools",
        )
        cooler_markers = (
            "hard cooler",
            "insulated cooler",
            "ice chest",
            "portable cooler",
            "cooler can",
            " coolers",
        )
        excluded_bench_markers = (
            "storage ottoman",
            "ottoman bench",
            "storage footstool",
            "garden bench",
            "outdoor bench",
            "park bench",
            "shower bench",
            "bath stool",
            "potting bench",
            "hall tree",
            "coat rack bench",
            "shoe bench",
        )
        indoor_bench_markers = (
            "dining bench",
            "long bench",
            "upholstered bench",
            "mid century bench",
            "bench with",
            "end of bed",
            "footrest stool",
        )
        kids_table_set_markers = (
            "kids activity table", "kids table", "kids table and chair", "kids table & chair",
            "children table and chair", "children's table and chair", "childrens table and chair",
            "play table and chair", "play table & chair", "activity table and chair", "toddler table and chair",
            "table and chair set for toddlers",
        )
        non_dining_table_set_markers = (
            "coffee table",
            "cocktail table",
            "side table",
            "end table",
            "console table",
            "nesting table",
        )
        outdoor_table_markers = (
            "patio dining table", "outdoor dining table", "bistro table", "garden table",
            "outdoor table", "patio table",
        )
        wine_storage_markers = ("wine rack", "wine cabinet", "stemware rack")
        hall_tree_markers = ("hall tree", "coat rack bench", "hall stand")

        if any(kw in title_lower for kw in pet_identity_terms):
            if any(marker in title_lower for marker in decor_markers):
                if cid not in decor_categories:
                    return False
            elif any(marker in title_lower for marker in pet_product_markers):
                if cid not in pet_categories and not any(kw in title_lower for kw in motors_keywords):
                    return False

        if cid in pet_categories and not any(marker in title_lower for marker in pet_product_markers):
            return False

        if any(marker in title_lower for marker in fountain_markers):
            if cid != "20507":
                return False

        if any(marker in title_lower for marker in bunk_bed_markers):
            return cid == "175758"

        if any(marker in title_lower for marker in bed_frame_markers) and not any(
            marker in title_lower for marker in bunk_bed_markers
        ):
            return cid == "175758"

        if (
            any(marker in title_lower for marker in sofa_markers)
            and not any(marker in title_lower for marker in patio_furniture_set_markers)
            and not any(marker in title_lower for marker in bean_bag_markers)
            and not any(marker in title_lower for marker in bunk_bed_markers + bed_frame_markers)
            and not any(
                marker in title_lower for marker in ("dog bed", "cat bed", "pet bed")
            )
        ):
            if cid not in sofa_categories:
                return False

        if any(marker in title_lower for marker in bean_bag_markers):
            if cid not in bean_bag_categories:
                return False

        has_outdoor_chair_title = (
            any(kw in title_lower for kw in ("outdoor", "patio", "garden", "backyard", "poolside", "deck", "balcony"))
            and not any(kw in title_lower for kw in ("sofa", "couch", "loveseat", "sectional"))
            and re.search(r"\btable\b", title_lower) is None
            and any(
                kw in title_lower
                for kw in (
                    "outdoor chair",
                    "patio chair",
                    "club chair",
                    "club chairs",
                    "armchair",
                    "armchairs",
                    "patio armchair",
                    "patio armchairs",
                    "outdoor dining chair",
                    "outdoor dining chairs",
                    "outdoor lounge chair",
                    "outdoor lounge chairs",
                    "patio lounge",
                    "sun lounger",
                    "camping chair",
                    "camping chairs",
                )
            )
        )
        if any(marker in title_lower for marker in patio_furniture_set_markers) and not has_outdoor_chair_title and not any(
            marker in title_lower for marker in dining_set_markers
        ):
            if cid not in patio_furniture_set_categories:
                return False

        if any(marker in title_lower for marker in shade_sail_markers):
            if cid not in shade_sail_categories:
                return False

        if any(marker in title_lower for marker in game_table_markers):
            if cid not in game_table_categories:
                return False

        if any(marker in title_lower for marker in table_tennis_markers):
            return cid in table_tennis_categories

        if any(marker in title_lower for marker in golf_putting_green_markers) and "golf" in title_lower:
            if cid not in putting_green_categories:
                return False

        if any(marker in title_lower for marker in golf_hitting_mat_markers) and not any(
            marker in title_lower for marker in golf_putting_green_markers
        ):
            if cid not in golf_mat_categories:
                return False

        if any(marker in title_lower for marker in bar_stool_markers):
            return cid == "103431"

        if any(marker in title_lower for marker in dining_set_markers) and not any(
            marker in title_lower for marker in non_dining_table_set_markers
        ) and not any(marker in title_lower for marker in bar_stool_markers):
            return cid in dining_set_categories

        if any(marker in title_lower for marker in cooler_markers) or re.search(r"\bcooler\b", title_lower):
            if cid not in cooler_categories:
                return False

        has_ottoman_title = any(marker in title_lower for marker in ottoman_markers + ("ottoman", "footstool", "pouf")) and not any(
            marker in title_lower for marker in sofa_markers + ("armchair", "accent chair", "reading chair", "club chair", "chaise lounge", "recliner", "manual reclining", "reclining footrest")
        )
        if has_ottoman_title:
            return cid in ottoman_categories

        has_indoor_bench_context = not any(
            marker in title_lower for marker in excluded_bench_markers
        ) and any(marker in title_lower for marker in indoor_bench_markers)
        if has_indoor_bench_context and cid not in indoor_bench_categories:
            return False

        if any(marker in title_lower for marker in kids_table_set_markers):
            if cid not in kids_table_set_categories:
                return False

        if any(marker in title_lower for marker in outdoor_table_markers) and not any(
            marker in title_lower for marker in dining_set_markers
        ):
            if cid not in outdoor_table_categories:
                return False

        has_bar_cabinet_wine_context = (
            "bar cabinet" in title_lower
            and "coffee bar cabinet" not in title_lower
            and any(kw in title_lower for kw in ("wine", "stemware", "liquor", "bottle"))
        )
        has_wine_storage_context = any(marker in title_lower for marker in wine_storage_markers) or has_bar_cabinet_wine_context
        if has_wine_storage_context:
            if cid not in wine_storage_categories:
                return False
        elif cid in wine_storage_categories:
            return False

        if any(marker in title_lower for marker in hall_tree_markers):
            if cid not in hall_tree_categories:
                return False

        if any(marker in title_lower for marker in porch_swing_markers) or (
            "swing bed" in title_lower
            and any(kw in title_lower for kw in ("outdoor", "patio", "garden", "backyard", "poolside", "porch"))
            and "rope" in title_lower
            and not any(kw in title_lower for kw in ("cat ", "dog ", "pet ", "kitten", "puppy"))
        ):
            if cid not in porch_swing_categories:
                return False

        if any(marker in title_lower for marker in frog_statuary_markers):
            if cid != "29511":
                return False

        if any(marker in title_lower for marker in garden_stool_markers):
            allowed_garden_stool_categories = {"79682", "79684"}
            if any(marker in title_lower for marker in frog_statuary_markers) or any(
                marker in title_lower for marker in statuary_markers
            ):
                allowed_garden_stool_categories.add("29511")
            if cid not in allowed_garden_stool_categories:
                return False

        if any(kw in title_lower for kw in golf_product_markers):
            if cid in pet_categories:
                return False

        if any(kw in title_lower for kw in outdoor_shelter_markers):
            if cid not in {"180994", "180995"}:
                return False

        if any(marker in title_lower for marker in trellis_markers) and "arch" not in title_lower:
            if cid not in trellis_categories:
                return False

        if any(marker in title_lower for marker in arbor_markers):
            if cid not in arbor_categories:
                return False

        if any(marker in title_lower for marker in luggage_markers):
            if cid not in luggage_categories:
                return False

        if any(marker in title_lower for marker in basketball_arcade_markers):
            if cid not in arcade_game_categories:
                return False

        if any(kw in title_lower for kw in indoor_recliner_markers):
            if cid in {"175758", "88057", "180994", "180995"}:
                return False

        zero_gravity_is_indoor_recliner = "zero gravity" in title_lower and any(
            kw in title_lower
            for kw in (
                "recliner",
                "air leather",
                "vibration",
                "heating",
                "heat",
                "massage",
            )
        )
        if zero_gravity_is_indoor_recliner and cid in outdoor_chair_categories:
            return False

        if any(kw in title_lower for kw in ("cat tree", "cat tower", "cat condo", "catio", "scratching post", "cat scratch")):
            if cid != "20740":
                return False

        if any(kw in title_lower for kw in ("cat cabinet", "cat retreat", "cat hideaway", "cat house", "litter box enclosure", "cat enclosure")):
            allowed = {"20740", "100411"}
            if cid not in allowed:
                return False

        if any(kw in title_lower for kw in ("cat swing bed", "cat hammock", "hanging cat bed", "pet hammock")):
            if cid not in {"149074", "66762", "20744"}:
                return False

        if "cat bed" in title_lower and cid not in {"66762", "149074", "20744"}:
            return False

        if any(kw in title_lower for kw in ("dog bed", "pet bed")):
            if cid not in {"20744", "149074", "66762"}:
                return False

        if "dog house" in title_lower and cid != "108884":
            return False

        if any(kw in title_lower for kw in ("dog ramp", "dog stairs", "pet stairs")):
            if cid not in {"116389", "75067"}:
                return False

        if any(kw in title_lower for kw in ("pet gate", "pet playpen", "dog pen", "exercise pen")):
            if cid not in {"20748", "117029"}:
                return False

        if any(kw in title_lower for kw in ("chicken coop", "poultry coop", "hen house", "chicken run")):
            if cid not in {"63108", "177801", "10864"}:
                return False

        if any(kw in title_lower for kw in ("rabbit hutch", "rabbit cage")):
            if cid != "63108":
                return False

        plant_stand_title = any(
            kw in title_lower
            for kw in (
                "plant stand",
                "plant pedestal",
                "garden pedestal",
                "roman column",
                "column pedestal",
                "pillar",
            )
        )
        if plant_stand_title and not any(marker in title_lower for marker in fountain_markers):
            allowed_plant_stand_categories = set(plant_stand_categories)
            if any(marker in title_lower for marker in arbor_markers):
                allowed_plant_stand_categories.update(arbor_categories)
            if cid not in allowed_plant_stand_categories:
                return False

        if any(kw in title_lower for kw in ("dump cart", "wagon", "wheelbarrow", "tow behind", "hauling", "cart trailer", "garden cart")):
            if cid not in cart_categories:
                return False

        if any(kw in title_lower for kw in statuary_markers) and not any(marker in title_lower for marker in fountain_markers):
            if cid not in decor_categories and cid not in plant_stand_categories:
                return False

        if any(kw in title_lower for kw in tent_keywords):
            if cid not in {"179010", "175750"}:
                return False

        if any(kw in title_lower for kw in grill_keywords):
            if cid != "151621":
                return False

        if any(kw in title_lower for kw in ("treadmill", "walking pad", "running machine")) and not any(
            kw in title_lower for kw in ("dog treadmill", "pet treadmill")
        ):
            return cid == "15280"

        if any(kw in title_lower for kw in motors_keywords):
            if cid not in motors_categories:
                return False

        if "adirondack" in title_lower and cid != "79684":
            return False

        has_outdoor_daybed_title = any(kw in title_lower for kw in ("outdoor daybed", "patio daybed", "sunbed")) or (
            "daybed" in title_lower
            and any(kw in title_lower for kw in ("outdoor", "patio", "garden", "backyard", "poolside"))
            and not (
                any(marker in title_lower for marker in porch_swing_markers)
                or ("swing bed" in title_lower and "rope" in title_lower)
            )
            and not any(kw in title_lower for kw in ("cat ", "dog ", "pet "))
        )
        if has_outdoor_daybed_title:
            if any(kw in title_lower for kw in ("sofa", "couch", "loveseat", "sectional")):
                if cid not in sofa_categories:
                    return False
            elif cid != "138996":
                return False

        if any(kw in title_lower for kw in (
            "outdoor chaise", "outdoor chaise lounge", "outdoor lounge chair", "patio lounge",
            "zero gravity", "pool lounge", "sun lounger",
        )) and not zero_gravity_is_indoor_recliner:
            if cid not in outdoor_chair_categories:
                return False

        if any(kw in title_lower for kw in ("outdoor chair", "patio chair", "egg chair", "hanging egg chair", "hanging swing chair")):
            if any(kw in title_lower for kw in ("cat", "dog", "pet")):
                return cid in pet_categories
            if cid not in outdoor_chair_categories:
                return False

        if (
            any(kw in title_lower for kw in ("club chair", "club chairs", "outdoor dining chair", "outdoor dining chairs"))
            and any(kw in title_lower for kw in ("outdoor", "patio", "garden", "backyard", "poolside", "deck", "balcony"))
            and not any(kw in title_lower for kw in ("sofa", "couch", "loveseat", "sectional"))
            and re.search(r"\btable\b", title_lower) is None
        ):
            if cid not in {"79682", "79684"}:
                return False

        if any(kw in title_lower for kw in (
            "computer desk", "writing desk", "executive desk", "standing desk", "desktop", "tabletop",
            "l-shaped desk", "l shaped desk", "corner desk",
        )) and "dining table" not in title_lower and not any(kw in title_lower for kw in outdoor_shelter_markers):
            if cid not in desk_categories:
                return False

        if any(kw in title_lower for kw in (
            "coffee table", "cocktail table", "nesting table", "pub table", "bar table",
        )):
            if any(marker in title_lower for marker in patio_furniture_set_markers):
                if cid not in patio_furniture_set_categories:
                    return False
                return True
            if cid != "38204":
                return False

        if any(kw in title_lower for kw in ("dining table", "farm table", "rectangular dining")):
            if any(marker in title_lower for marker in outdoor_table_markers):
                if cid not in outdoor_table_categories:
                    return False
            elif cid not in table_categories:
                return False

        if cid in table_categories and re.search(r"\btable\b", title_lower) is None:
            return False

        if any(kw in title_lower for kw in ("sandbox", "sandpit", "sand box")):
            if cid not in sandbox_categories:
                return False

        if cid == "38208":
            non_sofa_keywords = [
                'outdoor', 'patio', 'chaise lounge', 'pool cover', 'water slide',
                'inflatable', 'bounce house', 'trampoline', 'bar stool',
                'dining', 'bed frame', 'bunk bed', 'loft bed',
                'lamp', 'hall tree', 'fence', 'fire pit', 'garden fence',
                'tent', 'cat', 'dog', 'pet', 'desk', 'tabletop',
            ]
        if any(kw in title_lower for kw in ("sandbox", "sandpit", "sand box")):
            if cid not in sandbox_categories:
                return False

        if cid == "38208":
            non_sofa_keywords = [
                'outdoor', 'patio', 'chaise lounge', 'pool cover', 'water slide',
                'inflatable', 'bounce house', 'trampoline', 'bar stool',
                'dining', 'bed frame', 'bunk bed', 'loft bed',
                'lamp', 'hall tree', 'fence', 'fire pit', 'garden fence',
                'tent', 'cat', 'dog', 'pet', 'desk', 'tabletop',
            ]
            if any(kw in title_lower for kw in non_sofa_keywords):
                sofa_keywords = ['sofa', 'couch', 'loveseat', 'futon', 'recliner', 'sectional']
                if not any(sk in title_lower for sk in sofa_keywords):
                    return False

        if cid == "175758":
            if any(kw in title_lower for kw in ['sofa bed', 'pull out sofa', 'sleeper couch', 'convertible sofa']):
                return False
            # Bed Frames (175758) should not match tables/nightstands
            table_keywords = ['end table', 'side table', 'coffee table', 'nightstand', 'night stand', 'dining table']
            if any(kw in title_lower for kw in table_keywords) and not any(kw in title_lower for kw in bed_frame_markers + bunk_bed_markers):
                return False

        if cid == "54235":
            if 'bar stool' in title_lower or 'counter stool' in title_lower or 'barstool' in title_lower:
                return False
            if any(kw in title_lower for kw in ('outdoor', 'patio', 'adirondack', 'chaise', 'daybed')):
                return False
            if 'zero gravity' in title_lower and not zero_gravity_is_indoor_recliner:
                return False

        if cid == "36449":
            if any(kw in title_lower for kw in ['recliner', 'lift chair', 'lift recliner', 'armchair', 'chair', 'sofa']):
                return False

        if cid == "103431":
            if any(kw in title_lower for kw in ['hall tree', 'entryway bench', 'storage bench', 'shoe bench']):
                return False
            if has_indoor_bench_context:
                return False

        return True
    
    def _is_api_category_plausible(self, title: str, api_cat_id: str, api_cat_name: str) -> bool:
        """Check if API-suggested category makes sense for the product title.
        
        Rejects clearly wrong API suggestions like:
        - Outdoor furniture → Sofas (38208)
        - Pool covers → Sofas (38208)
        - Water slides → Sofas (38208)
        """
        return self.is_category_plausible_for_text(title, api_cat_id, api_cat_name)

    def _normalize_keyword_text(self, value: str) -> str:
        """Strip HTML noise before keyword matching so tags don't trigger false categories."""
        if not value:
            return ""
        text = html.unescape(value)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.lower().strip()
    
    def _fallback_category(self, title: str, description: str = None) -> Tuple[Optional[str], Optional[str]]:
        """Fallback category mapping based on keywords - comprehensive for GigaCloud products"""
        # Combine title and description for better matching
        title_lower = self._normalize_keyword_text(title)
        desc_lower = self._normalize_keyword_text((description or "")[:3000])
        
        mappings = [
            # ==================== MOTORS ====================
            (["running board", "running boards", "nerf bar", "nerf bars", "side step", "side steps", "step bar", "truck step"], "262210", "Running Boards & Nerf Bars"),
            (["trailer hitch", "receiver hitch"], "174020", "Trailer Hitches"),
            (["hitch cargo carrier", "cargo carrier hitch", "hitch carrier"], "174021", "Hitch Cargo Carriers"),
            (["roof rack", "cross bar", "cross bars", "crossbar", "cargo rack"], "262216", "Roof Racks & Cross Bars"),
            (["tailgate ladder", "tailgate handle", "tailgate assist", "tailgate utility"], "262093", "Tailgate Parts"),
            (["bike trailer"], "85040", "Bike Trailers"),

            # ==================== SPORTING GOODS ====================
            # Match golf before pet rules; "Golf Practice Net Cage" is not an animal cage.
            (["golf practice net", "golf training net", "golf hitting net", "hitting net", "golf batting cage", "batting cage", "golf simulator net"], "50876", "Nets, Cages & Mats"),
            (["golf putting green", "putting green"], "36234", "Putting Greens & Aids"),
            (["golf hitting mat", "golf practice mat", "golf training mat", "golf swing mat"], "50876", "Nets, Cages & Mats"),
            (["golf bag organizer", "golf club organizer", "golf clubs storage", "golf club storage", "golf bag rack", "golf club holder", "club holder"], "30109", "Golf Club Bags"),

            # ==================== PET SUPPLIES ====================
            # Dog Products
            (["dog crate", "dog kennel", "dog cage"], "121851", "Dog Cages & Crates"),
            (["dog bed"], "20744", "Beds"),
            (["dog house"], "108884", "Dog Houses"),
            (["dog ramp", "dog stairs", "pet stairs"], "116389", "Ramps & Stairs"),
            (["dog treadmill", "pet treadmill"], "116383", "Agility Training"),
            (["pet gate", "pet fence", "pet playpen", "dog pen"], "20748", "Fences & Exercise Pens"),
            (["pet stroller"], "116380", "Strollers"),
            (["pet carrier"], "177788", "Carriers & Totes"),
            
            # Cat Products
            (["cat tree", "cat tower", "cat condo", "catio"], "20740", "Furniture & Scratchers"),
            (["cat litter", "litter box", "litter box enclosure", "cat enclosure"], "100411", "Litter Boxes"),
            (["cat swing bed", "cat hammock", "hanging cat bed"], "149074", "Beds, Hammocks & Nesters"),
            (["cat bed"], "66762", "Beds"),
            (["scratching post", "cat scratch"], "20740", "Furniture & Scratchers"),
            (["cat cabinet", "cat retreat", "cat hideaway", "cat house"], "20740", "Furniture & Scratchers"),
            
            # Chicken/Poultry
            (["chicken coop", "chicken house", "poultry coop", "hen house", "chicken run"], "63108", "Cages, Hutches & Enclosure"),
            
            # Small Animals
            (["rabbit hutch", "rabbit cage"], "63108", "Cages, Hutches & Enclosure"),
            (["hamster cage", "guinea pig", "small animal cage"], "26684", "Small Animal Cages"),
            (["bird cage", "aviary"], "14769", "Bird Cages"),
            (["fish tank", "aquarium"], "77639", "Aquarium Tanks"),
            (["reptile cage", "terrarium"], "63112", "Reptile Cages"),
            
            # ==================== KIDS FURNITURE ====================
            (["race car bed", "car bed", "kids bed", "toddler bed", "children bed"], "175758", "Beds & Bed Frames"),
            (["bunk bed", "loft bed", "queen over queen", "twin over twin", "twin over full", "full over full"], "175758", "Beds & Bed Frames"),
            (["kids desk"], "25290", "Kids Desks"),
            (["kids chair"], "66758", "Kids Chairs"),
            (["kids activity table", "kids table and chair", "kids table & chair", "children table and chair", "children's table and chair", "play table and chair", "table and chair set for toddlers"], "66743", "Play Table & Chair Sets"),
            (["kids table"], "66743", "Play Table & Chair Sets"),
            (["toy storage", "toy box", "toy chest"], "66763", "Kids Storage"),
            (["changing table"], "20422", "Changing Tables"),
            (["crib", "baby crib"], "20421", "Cribs"),

            # ==================== OUTDOOR SPECIAL CASES ====================
            # Must come before indoor sofa / accent chair rules.
            (["patio furniture set", "patio set", "patio conversation", "outdoor conversation set", "outdoor furniture set", "outdoor sectional"], "139849", "Patio & Garden Furniture Sets"),
            (["patio dining table", "outdoor dining table", "bistro table", "garden table", "outdoor table", "patio table"], "112590", "Patio & Garden Tables"),
            (["garden arch", "rose arch", "garden arbor", "arbor arch", "arch with gate"], "180993", "Arbors & Arches"),
            (["garden trellis", "metal trellis", "plant trellis", "trellis for climbing", "climbing plants support"], "43538", "Trellises"),
            (["table tennis table", "ping pong table", "table tennis", "ping pong"], "97075", "Tables"),
            (["fountain", "water feature", "waterfall fountain", "cascading water"], "20507", "Outdoor Fountains"),
            (["frog garden stool", "garden frog", "frog statue", "frog sitting on rock"], "29511", "Ornaments & Statues"),
            (["garden stool", "outdoor stool", "patio stool"], "79682", "Patio Chairs"),
            (["camping tent", "camping tents", "inflatable tent", "inflatable tents", "inflatabletent", "glamping tent", "glamping tents", "blow up tent", "blow-up tent", "air tent", "suv tent"], "179010", "Tents"),
            (["bbq grill", "barbecue grill", "propane grill", "gas grill", "grill with side burner", "grill & griddle", "grill griddle"], "151621", "Barbecues, Grills & Smokers"),
            (["shade sail", "sun shade sail", "sunshade sail"], "180997", "Shade Sails"),
            (["gazebo", "outdoor canopy", "patio canopy", "garden canopy"], "180995", "Gazebos"),
            (["pergola"], "180994", "Pergolas"),
            (["egg chair", "egg swing chair", "hanging egg chair", "hanging swing chair", "hanging chair with stand", "wicker hanging swing chair", "patio hammock swing chair", "hanging basket chair"], "79682", "Patio Chairs"),
            (["outdoor daybed", "patio daybed", "sunbed"], "138996", "Outdoor Daybeds"),
            (["outdoor chair set", "patio chair set", "armchair set", "rattan chair set", "outdoor armchair", "outdoor armchairs", "patio armchair", "patio armchairs", "armchairs set"], "79682", "Patio Chairs"),
            (["outdoor folding chair", "patio folding chair", "outdoor folding chair set", "patio folding chair set", "camping chair", "camping chairs", "outdoor lounge chair", "outdoor lounge chairs", "patio lounge", "sun lounger"], "79684", "Outdoor Chairs"),
            (["porch swing bed", "patio swing bed", "garden swing bed"], "79694", "Porch Swings"),
            
            # ==================== SOFA BEDS (before Bedroom to avoid false match) ====================
            (["pull out sofa", "sofa bed", "sleeper sofa", "sleeper couch", "convertible sofa",
              "convertible sleeper"], "38208", "Sofas, Armchairs & Couches"),
            (["storage ottoman", "ottoman bench", "lift top ottoman", "storage footstool"], "20490", "Ottomans, Footstools & Poufs"),
            (["dining bench", "long bench", "upholstered bench", "mid century bench", "bench with", "end of bed", "footrest stool"], "262980", "Benches"),

            # Recliners/chairs must come before bedroom rules; "upholstered bedroom"
            # otherwise contains the substring "upholstered bed".
            (["power lift recliner", "lift recliner", "lift chair", "recliner chair", "manual reclining", "reclining footrest", "armchair", "accent chair"], "54235", "Chairs"),
            
            # ==================== BEDROOM ====================
            (["platform bed", "bed frame", "upholstered bed", "panel bed"], "175758", "Beds & Bed Frames"),
            (["canopy bed", "sleigh bed", "murphy bed", "daybed"], "175758", "Beds & Bed Frames"),
            (["house bed", "floor bed", "montessori bed"], "175758", "Beds & Bed Frames"),
            (["headboard"], "175756", "Headboards"),
            (["nightstand", "night stand", "bedside table"], "38199", "Nightstands"),
            (["dresser", "chest of drawer", "drawer chest"], "114397", "Dressers & Chests of Drawers"),
            (["wardrobe", "armoire", "closet"], "103430", "Armoires & Wardrobes"),
            (["clothes rack", "garment rack"], "175755", "Clothing Racks"),
            (["vanity", "makeup table"], "32878", "Vanities & Makeup Tables"),
            (["mirror"], "20580", "Mirrors"),
            
            # ==================== LIVING ROOM ====================
            # TV & Entertainment
            (["tv stand", "television stand", "entertainment center", "media console", "media center", "tv cabinet"], "20488", "TV Stands & Entertainment Units"),
            (["electric fireplace"], "175759", "Electric Fireplaces"),
            
            # Sofas & Seating
            (["bean bag"], "48319", "Bean Bags & Inflatables"),
            (["pull out sofa", "sofa bed", "sleeper sofa", "sleeper couch", "convertible sofa", "convertible sleeper"], "38208", "Sofas, Armchairs & Couches"),
            (["sectional", "sectional sofa"], "38208", "Sectional Sofas"),
            (["sofa", "couch", "loveseat", "futon"], "38208", "Sofas, Armchairs & Couches"),
            (["chaise lounge"], "38208", "Sofas"),
            (["power lift recliner", "lift recliner", "lift chair", "recliner chair"], "54235", "Chairs"),
            (["recliner", "power recliner", "push back recliner"], "38208", "Recliners"),
            (["massage chair"], "181270", "Massage Chairs"),
            (["gaming chair"], "22513", "Gaming Chairs"),
            (["round chair", "cushioned backrest", "compressible chair"], "54235", "Chairs"),
            (["accent chair", "arm chair", "lounge chair", "club chair"], "54235", "Chairs"),
            (["rocking chair", "glider"], "20877", "Rocking Chairs"),
            (["ottoman", "footstool", "pouf"], "20490", "Ottomans, Footstools & Poufs"),
            
            # Tables
            (["coffee table", "cocktail table"], "38204", "Coffee Tables"),
            (["board game table", "gaming table", "game table"], "38204", "Tables"),
            (["sofa table", "console table", "entry table", "hall table"], "38205", "Console Tables"),
            (["end table", "side table", "accent table", "lamp table"], "38200", "End Tables"),
            (["nesting table"], "38204", "Nesting Tables"),
            
            # Bookcases
            (["bookshelf", "bookcase", "book shelf"], "3199", "Bookcases"),
            (["cube storage", "cube organizer", "shelf unit", "etagere", "ladder shelf"], "3199", "Shelving Units"),
            (["wall shelf", "floating shelf"], "20487", "Wall Shelves"),
            (["kitchen pantry", "pantry cabinet", "freestanding pantry", "pantry storage", "storage cabinet", "kitchen hutch", "hutch cabinet", "microwave shelf"], "20487", "Cabinets & Cupboards"),
            (["display cabinet", "curio cabinet"], "20493", "Display Cabinets"),
            
            # ==================== DINING ROOM ====================
            # Bar stools first (higher specificity than "counter height" for dining)
            (["bar stool", "bar stools", "counter stool", "counter stools", "barstool", "stool cushion", "barstools"], "103431", "Bar Stools & Stools"),
            # More specific patterns first
            (["dining table set", "dining set", "piece dining set", "5-piece dining", "5 piece dining", "6-piece dining", "6 piece dining"], "107578", "Dining Sets"),
            (["farmhouse table", "farmhouse dining", "farm table"], "107578", "Dining Sets"),
            (["table and stool", "table with stool", "table set", "counter height"], "107578", "Dining Sets"),
            (["pub set", "bar set", "breakfast set", "nook set"], "107578", "Dining Sets"),
            (["dining table"], "38204", "Dining Tables"),
            (["dining set"], "107578", "Dining Sets"),
            (["dining chair", "counter chair"], "54235", "Chairs"),
            (["pub table", "bar table", "counter table"], "38204", "Tables"),
            (["kitchen island", "kitchen cart", "microwave cart", "baker rack"], "177000", "Kitchen Islands & Carts"),
            (["buffet", "sideboard", "credenza"], "183322", "Sideboards & Buffets"),
            (["china cabinet"], "38217", "China Cabinets"),
            (["hutch"], "20487", "Cabinets & Cupboards"),
            (["wine rack", "wine cabinet", "stemware rack", "bar cabinet"], "20689", "Wine Racks & Bottle Holders"),
            (["pantry", "pantry cabinet"], "42428", "Pantry Cabinets"),
            
            # ==================== OFFICE ====================
            (["office chair", "desk chair", "executive chair", "ergonomic chair", "task chair"], "54235", "Office Chairs"),
            (["computer desk", "writing desk", "executive desk", "l-shaped desk", "corner desk", "standing desk"], "88057", "Desks & Tables"),
            (["desktop", "tabletop"], "88057", "Desks"),
            (["desk"], "88057", "Desks"),
            (["file cabinet", "filing cabinet"], "25306", "Filing Cabinets"),
            (["printer stand", "monitor stand"], "111508", "Printer Stands"),
            
            # ==================== BATHROOM ====================
            (["bathroom vanity", "sink vanity"], "32878", "Bathroom Vanities"),
            (["bathroom cabinet", "medicine cabinet", "linen cabinet"], "42428", "Bathroom Cabinets"),
            (["bathroom mirror"], "133696", "Bathroom Mirrors"),
            (["towel rack", "towel bar"], "42427", "Towel Racks"),
            (["shower bench", "bath stool"], "42429", "Shower Benches"),
            (["laundry hamper", "laundry basket"], "43527", "Laundry Hampers"),
            
            # ==================== OUTDOOR ====================
            (["patio furniture set", "patio set", "patio conversation", "outdoor conversation set", "outdoor furniture set", "outdoor sectional"], "139849", "Patio & Garden Furniture Sets"),
            (["patio dining table", "outdoor dining table", "bistro table", "garden table", "outdoor table", "patio table"], "112590", "Patio & Garden Tables"),
            (["fountain", "water feature", "waterfall fountain", "cascading water"], "20507", "Outdoor Fountains"),
            (["frog garden stool", "garden frog", "frog statue", "frog sitting on rock"], "29511", "Ornaments & Statues"),
            (["garden stool", "outdoor stool", "patio stool"], "79682", "Patio Chairs"),
            (["outdoor daybed", "patio daybed", "sunbed"], "138996", "Outdoor Daybeds"),
            (["camping tent", "camping tents", "inflatable tent", "inflatable tents", "inflatabletent", "glamping tent", "glamping tents", "blow up tent", "blow-up tent", "air tent", "suv tent"], "179010", "Tents"),
            (["egg chair", "egg swing chair", "hanging egg chair", "hanging swing chair", "hanging chair with stand", "wicker hanging swing chair", "patio hammock swing chair", "hanging basket chair"], "79682", "Patio Chairs"),
            (["outdoor chair set", "patio chair set", "armchair set", "rattan chair set", "outdoor armchair", "outdoor armchairs", "patio armchair", "patio armchairs", "armchairs set"], "79682", "Patio Chairs"),
            (["outdoor chaise", "patio chaise", "outdoor lounge chair", "outdoor lounge chairs", "patio lounge", "sun lounger"], "79684", "Outdoor Chairs"),
            (["patio chat set", "patio conversation"], "139849", "Patio & Garden Furniture Sets"),
            (["patio set", "patio furniture", "outdoor sofa", "outdoor sectional"], "139849", "Patio & Garden Furniture Sets"),
            (["outdoor chair", "patio chair", "adirondack"], "79684", "Outdoor Chairs"),
            (["outdoor table", "patio table"], "112590", "Patio & Garden Tables"),
            (["garden bench", "park bench", "outdoor bench"], "79683", "Garden Benches"),
            (["porch swing bed", "patio swing bed", "garden swing bed"], "79694", "Porch Swings"),
            (["hall tree", "coat rack bench", "hall stand"], "261263", "Hall Trees & Stands"),
            (["storage bench", "entryway bench", "shoe bench"], "262980", "Benches"),
            (["porch swing"], "79694", "Porch Swings"),
            (["hammock"], "79693", "Hammocks"),
            (["shade sail", "sun shade sail", "sunshade sail"], "180997", "Shade Sails"),
            (["gazebo"], "180995", "Gazebos"),
            (["pergola", "canopy"], "180994", "Pergolas"),
            (["outdoor storage", "deck box"], "42430", "Outdoor Storage"),
            (["patio umbrella", "umbrella base"], "180998", "Patio Umbrellas"),
            (["fire pit", "outdoor fireplace"], "85916", "Fire Pits"),
            (["plant stand", "pedestal", "roman column", "pillar"], "29514", "Plant Stands"),
            (["solar lantern planter", "solar puppy planter", "solar dog planter"], "29511", "Ornaments & Statues"),
            (["flower pot planter", "planter statue"], "20518", "Baskets, Pots, Window Boxes & Saucers"),
            (["statue", "sculpture", "figurine", "ornament"], "29511", "Ornaments & Statues"),
            (["planter", "flower pot"], "20518", "Baskets, Pots, Window Boxes & Saucers"),
            (["greenhouse", "potting bench"], "139939", "Greenhouses"),
            (["raised bed", "garden bed"], "181017", "Raised Garden Beds"),
            (["fence panel", "garden fence", "privacy screen panel"], "139946", "Fence Panels"),
            (["dump cart", "garden cart", "wagon", "wheelbarrow", "tow behind"], "75671", "Wheelbarrows, Carts & Wagons"),

            # ==================== LIGHTING ====================
            (["floor lamp", "standing lamp", "crystal lamp"], "112581", "Lamps"),
            
            # ==================== POOL & WATER ====================
            (["pool cover", "pool fence", "pool reel"], "181068", "Pool Covers & Reels"),
            (["water slide", "bounce house", "inflatable water", "inflatable slide", "water park"], "145996", "Inflatable Bouncers"),
            (["inflatable bounce", "bouncy castle", "jumper house"], "145996", "Inflatable Bouncers"),
            (["hard cooler", "insulated cooler", "ice chest", "portable cooler", "cooler can", "cooler"], "79691", "Ice Chests & Coolers"),
            
            # ==================== SPORTS & FITNESS ====================
            (["stair stepper", "stepper machine", "spine twist stretcher", "twist stretcher", "waist trainer machine"], "28062", "Stair Machines & Steppers"),
            (["treadmill", "walking pad", "running machine"], "15280", "Treadmills"),
            (["exercise bike", "stationary bike", "spin bike"], "58102", "Exercise Bikes"),
            (["basketball arcade", "arcade basketball", "dual shot", "electronic scorer"], "36278", "Other Indoor Games"),
            (["trampoline"], "57275", "Trampolines"),
            (["sandbox", "sandpit", "sand box"], "145990", "Sandbox Toys and Sandboxes"),
            
            # ==================== STORAGE ====================
            (["storage cabinet", "utility cabinet", "garage cabinet"], "20487", "Cabinets & Cupboards"),
            (["hall tree"], "261263", "Hall Trees & Stands"),
            (["dining bench", "long bench", "upholstered bench", "mid century bench", "bench with", "end of bed", "footrest stool"], "262980", "Benches"),
            (["storage bench", "entryway bench", "shoe bench"], "262980", "Benches"),
            (["shoe rack", "shoe cabinet", "shoe bench", "shoe storage"], "38221", "Shoe Racks"),
            (["coat rack", "coat stand"], "32880", "Coat Racks"),
            (["umbrella stand"], "108044", "Umbrella Stands"),
            (["room divider", "partition", "screen"], "175764", "Room Dividers"),
            
            # ==================== TABLES (GENERAL) ====================
            (["folding table", "card table", "utility table"], "98044", "Folding Tables"),
            (["spinner wheel luggage", "spinner luggage", "hard shell luggage", "luggage set", "carry on suitcase", "travel suitcase", "checked luggage", "travel luggage"], "16080", "Luggage"),
            
            # Catch-all for common terms
            (["table"], "38204", "Tables"),
            (["chair"], "54235", "Chairs"),
            (["cabinet"], "38221", "Cabinets & Cupboards"),
        ]

        generic_mappings = mappings[-3:]
        specific_mappings = mappings[:-3]

        def _keyword_in_text(text: str, keyword: str) -> bool:
            keyword = (keyword or "").strip().lower()
            if not keyword or not text:
                return False
            return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None

        def match(text: str, rules) -> Tuple[Optional[str], Optional[str]]:
            for keywords, cat_id, cat_name in rules:
                if any(_keyword_in_text(text, kw) for kw in keywords):
                    return cat_id, cat_name
            return None, None

        for text, rules in (
            (title_lower, specific_mappings),
            (title_lower, generic_mappings),
            (f"{title_lower} {desc_lower}".strip(), specific_mappings),
        ):
            cat_id, cat_name = match(text, rules)
            if cat_id:
                return cat_id, cat_name
        
        # Default to generic Home & Garden
        return "38208", "Furniture"
    
    def _get_category_aspects(self, category_id: str) -> Tuple[List[Dict], List[Dict]]:
        """
        Get required and recommended aspects for a category
        Uses eBay Taxonomy API (Commerce)
        """
        if category_id in self._aspects_cache:
            cached = self._aspects_cache[category_id]
            required = [a for a in cached if a.get("required")]
            recommended = [a for a in cached if not a.get("required")]
            return required, recommended
        
        # Taxonomy is public catalog data: prefer the APPLICATION token. Sub-account
        # tokens carry only sell.* scopes (the keyset does not grant
        # commerce.taxonomy in the user-consent flow), so a user token returns 403
        # here and every aspect lookup silently degrades.
        token = None
        try:
            token = self.oauth.get_application_token()
        except Exception as app_err:
            logging.warning(f"[WARN] App token for aspects failed, trying user token: {app_err}")
            try:
                token = self.oauth.get_valid_token()
            except Exception as e:
                logging.warning(f"[WARN] Failed to get token for aspects: {e}")
                return [], []

        profile = get_store_profile()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": profile.ebay_marketplace_id,
        }

        # Auto parts live in the Motors tree (100); furniture/EBAY_US in tree 0.
        tree_id = profile.category_tree_id
        url = f"{self.base_url}/commerce/taxonomy/v1/category_tree/{tree_id}/get_item_aspects_for_category"
        params = {"category_id": category_id}
        
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            
            if resp.status_code != 200:
                logging.warning(f"[WARN] Aspects API failed: {resp.status_code}")
                return self._fallback_aspects(category_id)
            
            data = resp.json()
            aspects = data.get("aspects", [])
            
            # Parse aspects
            parsed = []
            for a in aspects:
                name = a.get("localizedAspectName", "")
                constraint = a.get("aspectConstraint", {})
                values = a.get("aspectValues", [])
                
                parsed.append({
                    "name": name,
                    "required": constraint.get("aspectRequired", False),
                    "mode": constraint.get("aspectMode", "FREE_TEXT"),
                    "allowed_values": [v.get("localizedValue") for v in values[:50]] if values else [],
                    "data_type": constraint.get("aspectDataType", "STRING")
                })
            
            # Cache it
            self._aspects_cache[category_id] = parsed
            
            required = [a for a in parsed if a.get("required")]
            recommended = [a for a in parsed if not a.get("required")]
            
            logging.info(f"[OK] Found {len(required)} required, {len(recommended)} recommended aspects")
            
            return required, recommended
            
        except Exception as e:
            logging.warning(f"[WARN] Failed to get aspects: {e}")
            return self._fallback_aspects(category_id)
    
    def _fallback_aspects(self, category_id: str) -> Tuple[List[Dict], List[Dict]]:
        """Fallback required aspects for common categories"""
        fallbacks = {
            # Sofas require Upholstery Fabric
            "38208": [
                {"name": "Upholstery Fabric", "required": True, "allowed_values": 
                 ["Velvet", "Linen", "Corduroy", "Leather", "Faux Leather", "Polyester", "Cotton", "Microfiber"]},
                {"name": "Brand", "required": True, "allowed_values": []},
                {"name": "Type", "required": False, "allowed_values": ["Sofa", "Sectional", "Loveseat"]},
            ],
            # Beds require Compatible Mattress Size
            "131604": [
                {"name": "Compatible Mattress Size", "required": True, "allowed_values": 
                 ["Twin", "Twin XL", "Full", "Queen", "King", "California King"]},
                {"name": "Size", "required": False, "allowed_values": 
                 ["Twin", "Twin XL", "Full", "Queen", "King", "California King"]},
                {"name": "Brand", "required": True, "allowed_values": []},
            ],
            # Dog crates - typical aspects
            "121851": [
                {"name": "Brand", "required": True, "allowed_values": []},
                {"name": "Type", "required": False, "allowed_values": ["Cage", "Crate", "Kennel"]},
                {"name": "Size", "required": False, "allowed_values": ["Small", "Medium", "Large", "XL", "XXL"]},
            ],
        }
        
        required = []
        recommended = []
        
        for aspect in fallbacks.get(category_id, []):
            if aspect.get("required"):
                required.append(aspect)
            else:
                recommended.append(aspect)
        
        return required, recommended
    
    def _complete_aspects(
        self, 
        existing: Dict[str, List[str]], 
        required: List[Dict], 
        recommended: List[Dict],
        title: str,
        category_name: str
    ) -> Dict[str, List[str]]:
        """
        Complete missing aspects based on existing data and smart defaults
        """
        completed = dict(existing)
        title_lower = title.lower()
        
        # Process required aspects first
        for aspect in required:
            name = aspect.get("name", "")
            is_measurement_aspect = name in {"Item Length", "Item Width", "Item Height", "Item Weight"}
            
            # Skip if already present
            if name in completed and completed[name]:
                continue
            
            # Try to auto-detect value
            value = self._detect_aspect_value(name, aspect, existing, title_lower, category_name)
            
            if value:
                completed[name] = [value] if isinstance(value, str) else value
                logging.info(f"[AUTO] Added required aspect: {name} = {value}")
            else:
                if is_measurement_aspect:
                    continue
                # Use default value from allowed values
                allowed = aspect.get("allowed_values", [])
                if allowed:
                    completed[name] = [allowed[0]]
                    logging.warning(f"[DEFAULT] Added default for {name}: {allowed[0]}")
        
        return completed
    
    def _detect_aspect_value(
        self, 
        aspect_name: str, 
        aspect: Dict, 
        existing: Dict, 
        title_lower: str,
        category_name: str
    ) -> Optional[str]:
        """Smart detection of aspect values"""
        
        # Map aspect name to detection logic
        detectors = {
            "Upholstery Fabric": self._detect_fabric,
            "Compatible Mattress Size": self._detect_size,
            "Size": self._detect_size,
            "Brand": self._detect_brand,
            "Color": self._detect_color,
            "Material": self._detect_material,
        }
        
        # Check if we have a detector for this aspect
        if aspect_name in detectors:
            return detectors[aspect_name](existing, title_lower, aspect.get("allowed_values", []))
        
        return None
    
    def _detect_fabric(self, existing: Dict, title: str, allowed: List[str]) -> Optional[str]:
        """Detect upholstery fabric from Material or title"""
        # Check existing Material aspect
        material = existing.get("Material", [""])[0].lower() if existing.get("Material") else ""
        
        fabric_map = {
            "velvet": "Velvet",
            "linen": "Linen", 
            "corduroy": "Corduroy",
            "leather": "Leather",
            "faux leather": "Faux Leather",
            "pu leather": "Faux Leather",
            "fabric": "Polyester",
            "polyester": "Polyester",
            "cotton": "Cotton",
            "microfiber": "Microfiber",
            "foam": "Polyester",  # foam seats usually have polyester cover
        }
        
        # Check material first
        for key, val in fabric_map.items():
            if key in material:
                return val
        
        # Check title
        for key, val in fabric_map.items():
            if key in title:
                return val
        
        # Default
        return "Polyester"
    
    def _detect_size(self, existing: Dict, title: str, allowed: List[str]) -> Optional[str]:
        """Detect bed/mattress size"""
        # Check existing Size aspect
        size = existing.get("Size", [""])[0].lower() if existing.get("Size") else ""
        
        size_map = {
            "twin xl": "Twin XL",
            "twin": "Twin",
            "full": "Full",
            "queen": "Queen",
            "king": "King",
            "california king": "California King",
            "cal king": "California King",
        }
        
        # Check existing size
        for key, val in size_map.items():
            if key in size:
                return val
        
        # Check title
        for key, val in size_map.items():
            if key in title:
                return val
        
        # Default for kids beds
        if "kid" in title or "child" in title or "race car" in title:
            return "Twin"
        
        return "Queen"  # Default
    
    def _detect_brand(self, existing: Dict, title: str, allowed: List[str]) -> Optional[str]:
        """Detect or create brand"""
        if existing.get("Brand"):
            return existing["Brand"][0]
        
        return "Unbranded"
    
    def _detect_color(self, existing: Dict, title: str, allowed: List[str]) -> Optional[str]:
        """Detect color from title or existing"""
        if existing.get("Color"):
            return existing["Color"][0]
        
        colors = ["red", "blue", "green", "black", "white", "gray", "grey", 
                  "brown", "beige", "yellow", "orange", "purple", "pink"]
        
        for color in colors:
            if color in title:
                return color.capitalize()
        
        return None
    
    def _detect_material(self, existing: Dict, title: str, allowed: List[str]) -> Optional[str]:
        """Detect material"""
        if existing.get("Material"):
            return existing["Material"][0]
        
        materials = {
            "wood": "Wood",
            "metal": "Metal",
            "plastic": "Plastic",
            "fabric": "Fabric",
            "leather": "Leather",
            "plywood": "Plywood",
            "mdf": "MDF",
            "steel": "Steel",
            "aluminum": "Aluminum",
        }
        
        for key, val in materials.items():
            if key in title:
                return val
        
        return None


# Convenience function
def create_category_matcher(environment: str = "PRODUCTION") -> EbayCategoryMatcher:
    """Create a category matcher instance"""
    oauth = EbayOAuthService(environment)
    return EbayCategoryMatcher(oauth)


if __name__ == "__main__":
    # Test the matcher
    logging.basicConfig(level=logging.INFO)
    
    matcher = create_category_matcher("PRODUCTION")
    
    test_products = [
        ("Modern Velvet Sofa 3 Seater Living Room Furniture", {"Material": ["Velvet"]}),
        ("Twin Size Race Car Bed for Kids - Red", {"Color": ["Red"]}),
        ("Large Dog Crate Heavy Duty Metal Kennel", {"Material": ["Metal"]}),
    ]
    
    for title, aspects in test_products:
        print(f"\n{'='*60}")
        print(f"Title: {title}")
        print(f"Existing: {aspects}")
        
        cat_id, cat_name, completed = matcher.get_category_and_aspects(title, aspects)
        
        print(f"Category: {cat_id} ({cat_name})")
        print(f"Completed Aspects: {list(completed.keys())}")
        
        # Show new aspects
        new_aspects = {k: v for k, v in completed.items() if k not in aspects}
        if new_aspects:
            print(f"Auto-added: {new_aspects}")
