"""
eBay Publisher Service

统一的发布服务，整合：
1. Category Auto-Matching (自动类目匹配)
2. Item Specifics Auto-Completion (自动填充必填属性)
3. Image URL Validation (图片URL验证)
4. Error Handling (错误处理)

架构设计:
- 单一职责: 只负责发布流程
- 依赖注入: 接收 OAuth 和 PolicyManager
- 错误恢复: 自动重试和修复
"""

import logging
import json
import re
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import time

from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager
from src.clients.real_ebay_client import RealEbayClient
from src.services.vehicle_compatibility import (
    CompatibilityAnalysis,
    analyze_ebay_motors_compatibility,
    apply_compatibility_aspects,
)
from src.utils.ebay_quantity import resolve_publish_quantity
from src.utils.publish_autofix import sanitize_placeholder_aspects, sanitize_single_value_aspects
from src.utils.publish_aspect_completion import (
    complete_publish_aspects,
    infer_bed_size,
    infer_set_includes,
    infer_upholstery_fabric,
)
from src.utils.html_truncator import smart_truncate_html
from src.utils.listing_quality_gate import enforce_store_brand_aspect


class PublishStatus(Enum):
    """发布状态枚举"""
    SUCCESS = "success"
    ERROR = "error"
    PENDING = "pending"
    NEEDS_REVIEW = "needs_review"


@dataclass
class PublishResult:
    """发布结果数据类"""
    status: PublishStatus
    message: str
    listing_id: Optional[str] = None
    offer_id: Optional[str] = None
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    aspects_added: Dict[str, List[str]] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "status": self.status.value,
            "message": self.message,
            "listing_id": self.listing_id,
            "offer_id": self.offer_id,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "aspects_added": self.aspects_added,
            "errors": self.errors
        }


class EbayPublisher:
    """
    统一的 eBay 发布服务
    
    Usage:
        publisher = EbayPublisher(oauth_service)
        result = publisher.publish(product_data)
    """
    
    # eBay API 限制常量
    MAX_TITLE_LENGTH = 80
    MAX_DESCRIPTION_LENGTH = 4000
    MAX_IMAGES = 12
    
    # eBay Motors categories (category tree 100, require EBAY_MOTORS marketplace)
    EBAY_MOTORS_CATEGORIES = {
        "174020", "174021", "262210", "262216", "262093",  # Auto parts
    }
    
    # 类目必填 Item Specifics 完整映射
    CATEGORY_REQUIRED_ASPECTS = {
        # Sofas, Armchairs & Couches (38208)
        "38208": {
            "required": ["Upholstery Fabric", "Brand", "Type", "Color", "Item Width", "Item Height", "Item Length"],
            "defaults": {
                "Upholstery Fabric": "Polyester",
                "Brand": "Unbranded",
                "Type": "Sofa",
                "Color": "Gray",
                "Item Width": "30 in",
                "Item Height": "32 in",
                "Item Length": "80 in"
            }
        },
        # Beds & Bedframes (131604)
        "131604": {
            "required": ["Compatible Mattress Size", "Brand", "Type", "Color"],
            "defaults": {
                "Compatible Mattress Size": "Twin",
                "Brand": "Unbranded",
                "Type": "Bed Frame",
                "Color": "Red"
            }
        },
        # Dog Cages & Crates (121851)
        "121851": {
            "required": ["Brand", "Type", "Material"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Cage",
                "Material": "Metal"
            }
        },
        # Coffee Tables / Dining Tables (38204)
        "38204": {
            "required": ["Brand", "Type", "Material", "Color", "Set Includes", "Item Width", "Item Height", "Item Length"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Coffee Table",
                "Material": "Wood",
                "Color": "Brown",
                "Set Includes": "Table",
                "Item Width": "24 in",
                "Item Height": "18 in",
                "Item Length": "48 in"
            }
        },
        # TV Stands (20488)
        "20488": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "TV Stand",
                "Material": "Wood",
                "Color": "Black"
            }
        },
        # Bed Frames (175758) - 需要 Compatible Mattress Size
        "175758": {
            "required": ["Compatible Mattress Size", "Brand", "Type", "Color", "Material"],
            "defaults": {
                "Compatible Mattress Size": "Queen",
                "Brand": "Unbranded",
                "Type": "Bed Frame",
                "Color": "Black",
                "Material": "Metal"
            }
        },
        # Kids Beds / Bunk Beds (175754)
        "175754": {
            "required": ["Compatible Mattress Size", "Brand", "Type", "Color", "Material"],
            "defaults": {
                "Compatible Mattress Size": "Twin",
                "Brand": "Unbranded",
                "Type": "Bunk Bed",
                "Color": "Black",
                "Material": "Metal"
            }
        },
        # Sideboards & Buffets (183322)
        "183322": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Sideboard",
                "Material": "Wood",
                "Color": "Brown"
            }
        },
        # Dining Sets (107578) — Home & Garden > Furniture > Dining Sets
        # NOTE: 177816 is a Bicycle Components category (NOT Dining Sets!)
        "107578": {
            "required": ["Brand", "Type", "Number of Items in Set", "Number of Pieces", "Material", "Set Includes", "Item Width", "Item Height", "Item Length"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Dining Set",
                "Number of Items in Set": "5",
                "Number of Pieces": "5",
                "Material": "Wood",
                "Set Includes": "Dining Table & Chairs",
                "Item Width": "30 in",
                "Item Height": "30 in",
                "Item Length": "48 in"
            }
        },
        # Display Cabinets (20493)
        "20493": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Display Cabinet",
                "Material": "Wood",
                "Color": "Brown"
            }
        },
        # Armoires & Wardrobes (103430) — current valid taxonomy leaf.
        "103430": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Armoire",
                "Material": "Wood",
                "Color": "White"
            }
        },
        # Legacy alias kept for backward compatibility in old records.
        "68240": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Armoire",
                "Material": "Wood",
                "Color": "White"
            }
        },
        # Dining Chairs → use Chairs (54235) defined below; 25458 is non-leaf
        # Cabinets & Cupboards (20487)
        "20487": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Cabinet",
                "Material": "Wood",
                "Color": "White"
            }
        },
        # ==================== LUGGAGE & TRAVEL ====================
        # Luggage (16080) - Verified by eBay Taxonomy API 2026-02-05
        "16080": {
            "required": ["Brand", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Material": "ABS/Polycarbonate",
                "Color": "Black"
            }
        },
        # Luggage Sets (106917) - Legacy, may not work
        "106917": {
            "required": ["Brand", "Material", "Color", "Number of Pieces"],
            "defaults": {
                "Brand": "Unbranded",
                "Material": "ABS/Polycarbonate",
                "Color": "Black",
                "Number of Pieces": "3"
            }
        },
        # Suitcases (16289)
        "16289": {
            "required": ["Brand", "Material", "Color", "Size", "Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Material": "ABS/Polycarbonate",
                "Color": "Black",
                "Size": "Medium (22-25 in)",
                "Type": "Spinner"
            }
        },
        # Hardside Luggage (169294)
        "169294": {
            "required": ["Brand", "Material", "Color", "Size"],
            "defaults": {
                "Brand": "Unbranded",
                "Material": "ABS/Polycarbonate",
                "Color": "Black",
                "Size": "Large (26-29 in)"
            }
        },
        # Softside Luggage (169295)
        "169295": {
            "required": ["Brand", "Material", "Color", "Size"],
            "defaults": {
                "Brand": "Unbranded",
                "Material": "Nylon",
                "Color": "Black",
                "Size": "Large (26-29 in)"
            }
        },
        # Carry-Ons (80177)
        "80177": {
            "required": ["Brand", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Material": "ABS/Polycarbonate",
                "Color": "Black"
            }
        },
        # Duffel Bags (16290)
        "16290": {
            "required": ["Brand", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Material": "Nylon",
                "Color": "Black"
            }
        },
        # Trampolines (57275)
        "57275": {
            "required": ["Brand", "Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Mini Trampoline"
            }
        },
        # Treadmills (15280)
        "15280": {
            "required": ["Brand", "Type", "Resistance Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Treadmill",
                "Resistance Type": "Electric"
            }
        },
        # Ottomans (20490) - verified by eBay API
        "20490": {
            "required": ["Brand", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Material": "Faux Leather",
                "Color": "Black"
            }
        },
        # NOTE: 177816 already defined above with full config (Set Includes + dimensions)
        # Chairs (54235)
        "54235": {
            "required": ["Brand", "Type", "Material"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Task Chair",
                "Material": "Fabric",
            }
        },
        # Mattresses (131588) - 需要 Firmness, Compatible Mattress Size, Size
        "131588": {
            "required": ["Brand", "Firmness", "Compatible Mattress Size", "Type", "Size"],
            "defaults": {
                "Brand": "Unbranded",
                "Firmness": "Medium",  # Plush/Medium/Firm
                "Compatible Mattress Size": "Queen",
                "Type": "Memory Foam",
                "Size": "Queen"  # Twin/Full/Queen/King/California King
            }
        },
        # Mattress Pads & Toppers (175751)
        "175751": {
            "required": ["Brand", "Compatible Mattress Size", "Type", "Size"],
            "defaults": {
                "Brand": "Unbranded",
                "Compatible Mattress Size": "Queen",
                "Type": "Mattress Pad",
                "Size": "Queen"
            }
        },
        # Air Mattresses (106198)
        "106198": {
            "required": ["Brand", "Firmness", "Compatible Mattress Size", "Type", "Size"],
            "defaults": {
                "Brand": "Unbranded",
                "Firmness": "Medium",
                "Compatible Mattress Size": "Queen",
                "Type": "Air Mattress",
                "Size": "Queen"
            }
        },
        # ==================== AUTO PARTS & ACCESSORIES ====================
        # Running Boards & Nerf Bars (262210) — eBay Motors > Exterior Parts
        "262210": {
            "required": ["Brand", "Placement on Vehicle", "Fitment Type", "Color", "Material"],
            "defaults": {
                "Brand": "Unbranded",
                "Placement on Vehicle": "Left, Right",
                "Fitment Type": "Direct Replacement",
                "Color": "Black",
                "Material": "Steel"
            }
        },
        # Trailer Hitches (174020) — eBay Motors > Towing & Hauling
        "174020": {
            "required": ["Brand", "Hitch Class", "Receiver Size", "Fitment Type", "Color", "Material"],
            "defaults": {
                "Brand": "Unbranded",
                "Hitch Class": "Class III",
                "Receiver Size": "2 in",
                "Fitment Type": "Direct Replacement",
                "Color": "Black",
                "Material": "Steel"
            }
        },
        # Hitch Cargo Carriers (174021) — eBay Motors > Towing & Hauling
        "174021": {
            "required": ["Brand", "Type", "Material", "Color", "Maximum Load Capacity"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Hitch Cargo Carrier",
                "Material": "Steel",
                "Color": "Black",
                "Maximum Load Capacity": "500 lb"
            }
        },
        # Roof Racks & Cross Bars (262216) — eBay Motors > Exterior Parts
        "262216": {
            "required": ["Brand", "Type", "Material", "Color", "Fitment Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Roof Rack",
                "Material": "Steel",
                "Color": "Black",
                "Fitment Type": "Universal"
            }
        },
        # Tailgate Parts (262093) — eBay Motors > Truck Parts
        "262093": {
            "required": ["Brand", "Type", "Color", "Material", "Fitment Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Tailgate Assist",
                "Color": "Black",
                "Material": "Steel",
                "Fitment Type": "Universal"
            }
        },
        # Bike Trailers (85040) — Sporting Goods > Cycling > Trailers
        "85040": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Cargo Trailer",
                "Material": "Steel",
                "Color": "Black"
            }
        },
        # ==================== SPORTS & FITNESS ====================
        # Exercise Bikes (58102)
        "58102": {
            "required": ["Brand", "Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Exercise Bike"
            }
        },
        # Ellipticals (72602)
        "72602": {
            "required": ["Brand", "Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Elliptical"
            }
        },
        # Rowing Machines (28060)
        "28060": {
            "required": ["Brand", "Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Rowing Machine"
            }
        },
        # Weight Benches / Home Gym (158916)
        "158916": {
            "required": ["Brand", "Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Weight Bench"
            }
        },
        # ==================== ADDITIONAL CATEGORIES ====================
        # Headboards & Footboards (175756)
        "175756": {
            "required": ["Compatible Mattress Size", "Brand", "Type", "Color", "Material"],
            "defaults": {
                "Compatible Mattress Size": "Queen",
                "Brand": "Unbranded",
                "Type": "Headboard",
                "Color": "Gray",
                "Material": "Upholstered"
            }
        },
        # End Tables (38200)
        "38200": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "End Table",
                "Material": "Wood",
                "Color": "Brown"
            }
        },
        # Mirrors (20580)
        "20580": {
            "required": ["Brand", "Type", "Shape", "Frame Material", "Item Width", "Item Height", "Item Length"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Wall Mirror",
                "Shape": "Rectangular",
                "Frame Material": "Metal",
                "Item Width": "24 in",
                "Item Height": "36 in",
                "Item Length": "1 in"
            }
        },
        # Chicken Coops & Hutches (116394)
        "116394": {
            "required": ["Brand", "Type", "Material"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Chicken Coop",
                "Material": "Wood"
            }
        },
        # Nets, Cages & Mats — Golf / Sports (50876)
        "50876": {
            "required": ["Brand", "Sport/Activity", "Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Sport/Activity": "Golf",
                "Type": "Practice Net"
            }
        },
        # Home Office Desks (88057)
        "88057": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Computer Desk",
                "Material": "Wood",
                "Color": "Brown"
            }
        },
        # Safes (20584) - Home & Garden > Home Improvement > Locks, Latches & Keys > Safes
        "20584": {
            "required": ["Brand", "Type", "Material", "Color", "Lock Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Safe",
                "Material": "Steel",
                "Color": "Black",
                "Lock Type": "Electronic"
            }
        },
        # Patio Chairs, Swings & Benches (79682)
        "79682": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Chaise Lounge",
                "Material": "Acacia Wood",
                "Color": "Natural"
            }
        },
        # Patio Chairs, Swings & Benches (79684) — Egg Chairs, Hanging Chairs
        "79684": {
            "required": ["Brand", "Type", "Material", "Color", "Item Width", "Item Height", "Item Length"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Egg Chair",
                "Material": "HDPE",
                "Color": "Black",
                "Item Width": "30 in",
                "Item Height": "50 in",
                "Item Length": "30 in"
            }
        },
        # Kitchen Carts (115753) — fallback for kitchen islands
        "115753": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Kitchen Cart",
                "Material": "Wood",
                "Color": "White"
            }
        },
        # Kitchen Islands (177000)
        "177000": {
            "required": ["Brand", "Type", "Material", "Color", "Base Material", "Voltage", "Item Width", "Item Height", "Item Length"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Kitchen Island",
                "Material": "Wood",
                "Color": "White",
                "Base Material": "Wood",
                "Voltage": "Does Not Apply",
                "Item Width": "20 in",
                "Item Height": "36 in",
                "Item Length": "48 in"
            }
        },
        # Medicine Cabinets (42428)
        "42428": {
            "required": ["Brand", "Type", "Material", "Color", "Item Width", "Item Height", "Item Length"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Medicine Cabinet",
                "Material": "MDF",
                "Color": "White",
                "Item Width": "24 in",
                "Item Height": "26 in",
                "Item Length": "6 in"
            }
        },
        # Bathroom Vanities (32878)
        "32878": {
            "required": ["Brand", "Type", "Material", "Color", "Item Width", "Item Height", "Item Length"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Bathroom Vanity",
                "Material": "Wood",
                "Color": "White",
                "Item Width": "20 in",
                "Item Height": "32 in",
                "Item Length": "18 in"
            }
        },
        # Cages, Hutches & Enclosures (63108) — pet supplies
        "63108": {
            "required": ["Brand", "Type", "Material"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Hutch",
                "Material": "Wood"
            }
        },
        # Dressers & Chests of Drawers (114397)
        "114397": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Dresser",
                "Material": "Wood",
                "Color": "Brown"
            }
        },
        # ==================== CATEGORIES ADDED FROM FIX AUDIT ====================
        # Accent Chairs (118218) — Swivel, Lounge, Club, Barrel chairs
        "118218": {
            "required": ["Brand", "Type", "Material", "Color", "Upholstery Fabric"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Accent Chair",
                "Material": "Fabric",
                "Color": "Gray",
                "Upholstery Fabric": "Velvet"
            }
        },
        # Bar Stools & Stools (103431)
        "103431": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Bar Stool",
                "Material": "Metal",
                "Color": "Black"
            }
        },
        # Rocking Chairs (20877) — Gliders, Nursery Gliders
        "20877": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Glider",
                "Material": "Fabric",
                "Color": "Gray"
            }
        },
        # Hall Trees (22656) — Coat racks with storage
        "22656": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Hall Tree",
                "Material": "Wood",
                "Color": "White"
            }
        },
        # Hall Trees & Stands (261263)
        "261263": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Hall Tree",
                "Material": "Wood",
                "Color": "Brown"
            }
        },
        # Benches (262980)
        "262980": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Bench",
                "Material": "Wood",
                "Color": "Brown"
            }
        },
        # Bathroom Fixtures (115625)
        "115625": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Vanity",
                "Material": "Wood",
                "Color": "White"
            }
        },
        # Desks & Home Office (30889)
        "30889": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Desk",
                "Material": "Wood",
                "Color": "Brown"
            }
        },
        # Office Furniture (257900)
        "257900": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Office Chair",
                "Material": "Mesh",
                "Color": "Black"
            }
        },
        # Building Materials / Kitchen (20608)
        "20608": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Kitchen Island",
                "Material": "Wood",
                "Color": "White"
            }
        },
        # Bookcases & Shelving (3199)
        "3199": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Bookcase",
                "Material": "Wood",
                "Color": "Brown"
            }
        },
        # Fire Pits & Chimineas (181000)
        "181000": {
            "required": ["Brand", "Type", "Material", "Color", "Fuel Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Fire Pit",
                "Material": "Steel",
                "Color": "Black",
                "Fuel Type": "Propane"
            }
        },
        # Fire Pits (85916)
        "85916": {
            "required": ["Brand", "Type", "Material", "Color", "Fuel Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Fire Pit",
                "Material": "Steel",
                "Color": "Black",
                "Fuel Type": "Propane"
            }
        },
        # Lamps (112581)
        "112581": {
            "required": ["Brand", "Type", "Color", "Style"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Floor Lamp",
                "Color": "Gold",
                "Style": "Modern"
            }
        },
        # Planters (20497)
        "20497": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Planter",
                "Material": "MGO",
                "Color": "Gray"
            }
        },
        # Table Tennis Tables (159128)
        "159128": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Table Tennis Table",
                "Material": "MDF",
                "Color": "Blue"
            }
        },
        # Pool Covers & Reels (181068)
        "181068": {
            "required": ["Brand", "Type", "Material"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Pool Cover",
                "Material": "PVC"
            }
        },
        # Barbecues, Grills & Smokers (151621)
        "151621": {
            "required": ["Brand", "Type", "Fuel Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Propane Grill",
                "Fuel Type": "Propane",
                "Material": "Stainless Steel",
                "Color": "Black"
            }
        },
        # Tents (179010)
        "179010": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Camping Tent",
                "Material": "Oxford Fabric",
                "Color": "Beige",
                "Insect Repellent Treated": "No"
            }
        },
        # Inflatable Bouncers (145996)
        "145996": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Bounce House",
                "Material": "Oxford Fabric",
                "Color": "Multicolor"
            }
        },
        # Patio Chairs & Lounges (79682)
        "79682": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Outdoor Chair",
                "Material": "Acacia Wood",
                "Color": "Natural"
            }
        },
        # ==================== NEW PRODUCT CATEGORIES ====================
        # Shower Doors (147147) — Home & Garden > Bath > Showers
        "147147": {
            "required": ["Brand", "Type", "Material", "Color", "Door Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Shower Door",
                "Material": "Tempered Glass",
                "Color": "Clear",
                "Door Type": "Pivot"
            }
        },
        # MIG Welders (124822) — Business & Industrial > Welding Equipment
        "124822": {
            "required": ["Brand", "Type", "Power Source", "Voltage"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "MIG",
                "Power Source": "Electric",
                "Voltage": "110V/220V"
            }
        },
        # TIG Welders (124825)
        "124825": {
            "required": ["Brand", "Type", "Power Source", "Voltage"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "TIG",
                "Power Source": "Electric",
                "Voltage": "110V/220V"
            }
        },
        # Lawn Mowers (260921) — Home & Garden > Lawn Mowers
        "260921": {
            "required": ["Brand", "Type", "Power Source", "Cutting Width"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Reel/Push Mower",
                "Power Source": "Manual",
                "Cutting Width": "14 in"
            }
        },
        # Seeders & Spreaders (118869) — Garden Hand Tools
        "118869": {
            "required": ["Brand", "Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Spreader"
            }
        },
        # Chandeliers & Ceiling Fixtures (117503) — Lamps & Lighting
        "117503": {
            "required": ["Brand", "Type", "Style", "Color", "Number of Lights"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Chandelier",
                "Style": "Modern",
                "Color": "Gold",
                "Number of Lights": "6"
            }
        },
        # Inflatable Bouncers (145979) — Toys > Outdoor Toys
        "145979": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Inflatable Bouncer",
                "Material": "Oxford Fabric",
                "Color": "Multicolor"
            }
        },
        # Wheelbarrows, Carts & Wagons (75671) — Garden Hand Tools
        "75671": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Wagon",
                "Material": "Steel",
                "Color": "Black"
            }
        },
        # Golf Club Bags (30109) — Sporting Goods > Golf
        "30109": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Bag Organizer",
                "Material": "Wood",
                "Color": "Natural"
            }
        },
        # Ice Chests & Coolers (79691) — Home & Garden > Yard > Outdoor Cooking
        "79691": {
            "required": ["Brand", "Type", "Material", "Color", "Capacity"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Hard Cooler",
                "Material": "Plastic",
                "Color": "White",
                "Capacity": "25 qt"
            }
        },
        # Patio & Garden Furniture Sets (139849) — replaces non-leaf 25863
        "139849": {
            "required": ["Brand", "Type", "Material", "Color"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Patio Furniture Set",
                "Material": "PE Rattan",
                "Color": "Brown"
            }
        },
        # Shade Sails (180997) — Home & Garden > Yard, Garden & Outdoor Living
        "180997": {
            "required": ["Brand"],
            "defaults": {
                "Brand": "Unbranded",
                "Material": "Polyester",
                "Color": "Brown",
                "Shape": "Triangle"
            }
        },
        # Bean Bags & Inflatables (48319) — Home & Garden > Furniture
        "48319": {
            "required": ["Brand", "Type"],
            "defaults": {
                "Brand": "Unbranded",
                "Type": "Beanbag",
                "Department": "Adults",
                "Seating Capacity": "1"
            }
        },
    }
    
    # 类目关键词映射 - 全面覆盖 GigaCloud 产品类型
    CATEGORY_KEYWORDS = {
        # Luggage & Travel
        "106917": ["luggage set", "suitcase set", "travel set"],
        "16289": ["suitcase", "luggage", "spinner", "rolling luggage"],
        "169294": ["hardshell", "hardside", "hard luggage"],
        "169295": ["softside", "soft luggage"],
        "80177": ["carry on", "cabin bag", "carry-on"],
        "16290": ["duffle bag", "duffel bag", "travel bag"],
        
        # Pet Supplies
        "121851": ["dog crate", "dog kennel", "dog cage"],
        "20740": ["cat tree", "cat tower", "cat condo"],
        "100411": ["litter box", "cat litter", "litter box enclosure"],
        "63108": ["chicken coop", "chicken house", "poultry coop", "hen house"],
        "158938": ["rabbit hutch", "rabbit cage"],
        "20748": ["pet gate"],
        "108884": ["dog house"],
        "20744": ["dog bed", "pet bed"],
        "177788": ["pet carrier"],
        "116380": ["pet stroller"],
        "14769": ["bird cage", "aviary"],
        
        # Kids Furniture
        "175754": ["race car bed", "car bed", "kids bed", "bunk bed", "loft bed", "toddler bed"],
        "25290": ["kids desk"],
        "66758": ["kids chair"],
        "66756": ["kids table"],
        "66763": ["toy storage", "toy box", "toy chest"],
        "20421": ["crib", "baby crib"],
        
        # Bedroom
        "175758": ["bed frame", "platform bed", "upholstered bed", "daybed", "murphy bed"],
        "175756": ["headboard"],
        "38199": ["nightstand", "night stand", "bedside table"],
        "114397": ["dresser", "chest of drawer"],
        "103430": ["wardrobe", "armoire"],
        "175755": ["clothes rack", "garment rack"],
        
        # Living Room - TV & Entertainment
        "20488": ["tv stand", "entertainment center", "media console", "tv cabinet"],
        "175759": ["electric fireplace"],
        
        # Living Room - Sofas
        "38208": ["sofa", "couch", "loveseat", "sectional", "futon", "recliner"],
        "181270": ["massage chair"],
        "22513": ["gaming chair"],
        "20877": ["rocking chair", "glider"],
        "48319": ["bean bag"],
        "175761": ["ottoman", "footstool", "pouf"],
        
        # Living Room - Tables
        "38204": ["coffee table", "dining table", "board game table", "gaming table", "game table", "table", "pub table", "bar table", "counter table"],
        "38205": ["console table", "sofa table", "entry table", "hall table"],
        "38200": ["end table", "side table", "accent table"],
        
        # Living Room - Storage
        "3199": ["bookshelf", "bookcase", "cube storage", "etagere", "ladder shelf"],
        "20487": ["wall shelf", "floating shelf", "storage cabinet"],
        "20493": ["display cabinet", "curio cabinet"],
        
        # Dining Room
        "107578": ["dining set"],
        # NOTE: 54235 (Chairs) dining keywords merged into office section below
        "103431": ["bar stool", "counter stool", "barstool"],
        "177000": ["kitchen island", "kitchen cart", "microwave cart", "baker rack"],
        "183322": ["buffet", "sideboard", "credenza"],
        "38217": ["china cabinet", "hutch"],
        "45331": ["wine rack", "wine cabinet", "bar cabinet"],
        "42428": ["pantry", "pantry cabinet", "bathroom cabinet", "medicine cabinet", "linen cabinet"],
        
        # Office
        "54235": ["office chair", "desk chair", "executive chair", "ergonomic chair", "chair", "dining chair", "counter chair", "accent chair", "arm chair", "lounge chair", "club chair", "papasan"],
        "88057": ["desk", "computer desk", "writing desk", "standing desk", "l-shaped desk"],
        "25306": ["file cabinet", "filing cabinet"],
        "111508": ["printer stand", "monitor stand"],
        
        # Bathroom
        "32878": ["vanity", "bathroom vanity", "sink vanity"],
        "133696": ["bathroom mirror"],
        "42427": ["towel rack", "towel bar"],
        "42429": ["shower bench", "bath stool"],
        "43527": ["laundry hamper", "laundry basket"],
        
        # Outdoor
        "139849": ["patio furniture", "patio set", "outdoor sofa", "outdoor sectional"],
        "79682": ["outdoor chair", "adirondack", "outdoor chaise", "patio chaise", "sun lounger", "outdoor lounge chair", "patio lounge", "patio recliner"],
        "79686": ["outdoor table", "patio table"],
        "79683": ["garden bench", "park bench", "outdoor bench"],
        "79694": ["porch swing"],
        "79693": ["hammock"],
        "180997": ["shade sail", "sun shade sail", "sunshade sail"],
        "180995": ["gazebo"],
        "180994": ["pergola", "canopy"],
        "42430": ["outdoor storage", "deck box"],
        "180998": ["patio umbrella", "umbrella base"],
        "181000": ["fire pit", "outdoor fireplace"],
        "85916": ["fire pit", "outdoor fireplace"],
        "20518": ["planter"],
        "29514": ["plant stand", "pedestal", "column"],
        "139939": ["greenhouse", "potting bench"],
        "181017": ["raised bed", "garden bed"],
        "139946": ["fence panel", "garden fence", "privacy screen panel"],
        
        # Storage
        "261263": ["hall tree"],
        "262980": ["storage bench", "entryway bench", "shoe bench"],
        "38221": ["shoe rack", "shoe cabinet", "shoe bench", "shoe storage", "cabinet"],
        "32880": ["coat rack", "coat stand"],
        "108044": ["umbrella stand"],
        "20584": ["safe", "gun safe", "security safe", "safe box", "lockbox"],
        "175764": ["room divider", "partition", "screen"],
        
        # Tables
        "98044": ["folding table", "card table", "utility table"],
        
        # Mattresses (NEW)
        "131588": ["mattress", "memory foam", "hybrid mattress", "pocket spring"],
        "106198": ["air mattress", "inflatable bed", "blow up bed"],
        "175749": ["box spring", "mattress foundation"],
        "175750": ["mattress topper", "mattress pad", "mattress protector"],
        # Sports & Fitness
        "15280": ["treadmill", "running machine", "walking machine"],
        "58102": ["exercise bike", "stationary bike", "spin bike"],
        "72602": ["elliptical", "cross trainer"],
        "28060": ["rowing machine", "rower"],
        "158916": ["weight bench", "power rack", "home gym"],
        "159128": ["ping pong", "table tennis", "beer pong table"],
        # Pool & Water
        "181068": ["pool cover", "pool fence", "pool reel", "pool dome", "pool enclosure"],
        "145979": ["bounce house", "inflatable water", "inflatable slide", "water park", "bouncy castle", "inflatable bounce"],
        # Lighting
        "112581": ["floor lamp", "standing lamp", "crystal lamp"],
        # Outdoor Seating (already in 79682 above)
        
        # ==================== NEW PRODUCT TYPES ====================
        # Welding Equipment
        "124822": ["mig welder", "mig welding", "welder", "welding machine"],
        "124825": ["tig welder", "tig welding", "pulse welder"],
        # Shower & Bath
        "147147": ["shower door", "pivot shower", "shower glass", "shower enclosure"],
        # Lawn & Garden Equipment
        "260921": ["lawn mower", "reel mower", "push mower"],
        "118869": ["compost spreader", "peat moss spreader", "lawn spreader", "seed spreader"],
        # Lighting
        "117503": ["chandelier", "pendant light", "ceiling light", "starburst", "ceiling fixture"],
        # Wagons & Carts
        "75671": ["wagon cart", "folding wagon", "utility wagon", "collapsible wagon"],
        # Golf Equipment
        "30109": ["golf bag", "golf organizer", "golf storage"],
        # Ice Chests & Coolers
        "79691": ["cooler", "ice chest", "insulated cooler"],
    }
    
    def __init__(self, oauth_service: EbayOAuthService):
        """初始化发布服务"""
        self.oauth = oauth_service
        self.policy_manager = EbayPolicyManager(oauth_service)
        self.ebay_client = RealEbayClient(oauth_service, self.policy_manager)
        self.logger = logging.getLogger(__name__)
        
    def publish(self, product: Dict) -> PublishResult:
        """
        发布产品到 eBay
        """
        try:
            # 1. 验证基本数据
            validation_result = self._validate_product(product)
            if validation_result:
                return validation_result
            
            sku = product["sku"]
            
            # 2. 检查是否已有 Offer (可能是之前创建但未发布的)
            existing_offer = self._get_existing_offer(sku)
            
            if existing_offer:
                offer_id = existing_offer.get("offerId")
                offer_status = existing_offer.get("status", "")
                listing = existing_offer.get("listing", {})
                listing_status = listing.get("listingStatus", "")
                
                self.logger.info(f"[INFO] Found existing offer: {offer_id}, status: {offer_status}, listing: {listing_status}")
                
                # 如果已经是活跃的 listing，直接返回成功
                if listing_status == "ACTIVE":
                    return PublishResult(
                        status=PublishStatus.SUCCESS,
                        message=f"产品已在 eBay 上架! Listing ID: {listing.get('listingId')}",
                        listing_id=listing.get('listingId'),
                        offer_id=offer_id,
                        category_id=existing_offer.get('categoryId')
                    )
                
                # 如果 Offer 存在但未发布或 listing 已结束，尝试重新发布
                if offer_status == "UNPUBLISHED" or listing_status in ["ENDED", ""]:
                    # 先更新 inventory item 和 offer
                    publish_data = self._prepare_publish_data(product)
                    if not publish_data.get("images"):
                        return PublishResult(
                            status=PublishStatus.ERROR,
                            message="Missing images; cannot publish. Please sync images or check image URLs.",
                            errors=["imageUrls cannot be null or empty"]
                        )
                    
                    # 优先从 optimization 数据中获取 AI 确定的 Category ID
                    suggested_category_id = product.get("optimization", {}).get("categoryId")
                    if not suggested_category_id:
                        suggested_category_id = product.get("categoryId")
                        
                    category_id, category_name, completed_aspects = self._get_category_and_aspects(
                        publish_data["title"],
                        publish_data["aspects"],
                        product,
                        suggested_category_id
                    )
                    publish_data["aspects"] = completed_aspects
                    compatibility = self._prepare_motors_compatibility(
                        category_id,
                        publish_data["title"],
                        publish_data["description"],
                        publish_data["aspects"],
                    )
                    publish_data["aspects"] = apply_compatibility_aspects(
                        category_id,
                        publish_data["aspects"],
                        compatibility,
                    )
                    
                    # 更新 inventory item
                    self._create_inventory_item(sku, publish_data)
                    self._sync_inventory_compatibility(sku, compatibility)
                    
                    # 更新 offer 类目与 buyer-facing 描述
                    current_category = existing_offer.get("categoryId", "")
                    if current_category != category_id:
                        self.logger.info(f"[INFO] Updating offer category: {current_category} -> {category_id}")
                    self.ebay_client.update_offer_category(
                        offer_id,
                        category_id,
                        publish_data["price"],
                        listing_description=publish_data["offer_description"],
                    )
                    
                    # 发布 offer
                    return self._do_publish(offer_id, category_id, category_name, completed_aspects)
            
            # 3. 准备发布数据（新产品）
            publish_data = self._prepare_publish_data(product)
            if not publish_data.get("images"):
                return PublishResult(
                    status=PublishStatus.ERROR,
                    message="Missing images; cannot publish. Please sync images or check image URLs.",
                    errors=["imageUrls cannot be null or empty"]
                )
            
            # 4. 自动匹配类目和 Item Specifics
            suggested_category_id = product.get("optimization", {}).get("categoryId")
            if not suggested_category_id:
                suggested_category_id = product.get("categoryId")
                
            category_id, category_name, completed_aspects = self._get_category_and_aspects(
                publish_data["title"],
                publish_data["aspects"],
                product,
                suggested_category_id
            )
            
            publish_data["category_id"] = category_id
            publish_data["category_name"] = category_name
            publish_data["aspects"] = completed_aspects
            compatibility = self._prepare_motors_compatibility(
                category_id,
                publish_data["title"],
                publish_data["description"],
                publish_data["aspects"],
            )
            publish_data["aspects"] = apply_compatibility_aspects(
                category_id,
                publish_data["aspects"],
                compatibility,
            )
            
            # 5. 创建/更新 Inventory Item
            self._create_inventory_item(sku, publish_data)
            self._sync_inventory_compatibility(sku, compatibility)
            
            # 6. 创建 Offer
            offer_result = self._create_offer(
                sku,
                publish_data["price"],
                category_id,
                publish_data["offer_description"],
            )
            
            offer_id = offer_result.get("offerId")
            if not offer_id:
                return PublishResult(
                    status=PublishStatus.ERROR,
                    message="创建 Offer 失败",
                    errors=["No offer ID returned"]
                )
            
            # 7. 发布 Offer
            return self._do_publish(offer_id, category_id, category_name, completed_aspects)
                
        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"[PUBLISH ERROR] {error_msg}")
            
            # --- 自动恢复策略: Invalid Category ---
            normalized_error = error_msg.lower()
            if (
                "invalid category" in normalized_error
                or "invalid category id" in normalized_error
                or "not a leaf category" in normalized_error
                or "25005" in error_msg
                or "20400" in error_msg
            ):
                self.logger.warning(f"⚠️ Category Issue Detected. Attempting global auto-fix.")
                try:
                    # 尝试获取 offer_id
                    target_offer_id = None
                    if 'offer_id' in locals() and offer_id:
                        target_offer_id = offer_id
                    else:
                        # 尝试通过 SKU 查找
                        existing = self._get_existing_offer(sku)
                        if existing:
                            target_offer_id = existing.get("offerId")
                    
                    if target_offer_id:
                        self.logger.info(f"[AUTO-FIX] Deleting Offer {target_offer_id} to clear invalid category state...")
                        self.ebay_client.delete_offer(target_offer_id)
                        
                        return PublishResult(
                            status=PublishStatus.ERROR,
                            message=f"已自动清理无效类目 Offer。请 **立即重试**，下次将成功发布。",
                            errors=["Auto-fixed: Old offer deleted."]
                        )
                except Exception as fix_e:
                    self.logger.error(f"Auto-fix failed: {fix_e}")
            
            # 解析错误信息，提供更友好的提示
            friendly_msg = self._parse_error_message(error_msg)
            
            return PublishResult(
                status=PublishStatus.ERROR,
                message=f"Failed to publish: {friendly_msg} ({error_msg})",
                errors=[error_msg]
            )
    
    def _get_existing_offer(self, sku: str) -> Optional[Dict]:
        """获取 SKU 的现有 Offer"""
        try:
            token = self.oauth.get_valid_token()
            url = f"{self.oauth.api_base}/sell/inventory/v1/offer?sku={sku}"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            }
            
            import requests
            session = requests.Session()
            session.trust_env = False
            
            response = session.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                offers = data.get("offers", [])
                if offers:
                    return offers[0]  # 返回第一个 offer
            
            return None
        except Exception as e:
            self.logger.warning(f"[WARN] Failed to check existing offer: {e}")
            return None
    
    def _do_publish(
        self, 
        offer_id: str, 
        category_id: str, 
        category_name: str = "", 
        aspects: Dict = None
    ) -> PublishResult:
        """执行发布操作"""
        try:
            publish_result = self._publish_offer(offer_id)
            listing_id = publish_result.get("listingId")
            
            if listing_id:
                return PublishResult(
                    status=PublishStatus.SUCCESS,
                    message=f"发布成功! Listing ID: {listing_id}",
                    listing_id=listing_id,
                    offer_id=offer_id,
                    category_id=category_id,
                    category_name=category_name,
                    aspects_added=aspects or {}
                )
            else:
                return PublishResult(
                    status=PublishStatus.PENDING,
                    message=f"Offer 已创建，等待发布。Offer ID: {offer_id}",
                    offer_id=offer_id,
                    category_id=category_id
                )
        except Exception as e:
            error_msg = str(e)
            
            # CRITICAL RETRY LOGIC for Invalid Category
            normalized_error = error_msg.lower()
            if (
                "invalid category" in normalized_error
                or "invalid category id" in normalized_error
                or "not a leaf category" in normalized_error
                or "25005" in error_msg
                or "20400" in error_msg
            ):
                self.logger.warning(f"[AUTO-FIX] Invalid Category {category_id} detected. Deleting and Recreating Offer {offer_id}...")
                try:
                    # 1. Delete broken offer
                    self.ebay_client.delete_offer(offer_id)
                    time.sleep(1)
                    
                    return PublishResult(
                        status=PublishStatus.ERROR,
                        message=f"检测到旧Offer类目错误，已自动清理。请 **立即重试**，下次将成功发布。",
                        offer_id=offer_id,
                        errors=["Old offer deleted due to category error. Please retry."]
                    )
                except Exception as del_e:
                    self.logger.error(f"Failed to auto-delete offer: {del_e}")

            return PublishResult(
                status=PublishStatus.ERROR,
                message=self._parse_error_message(error_msg),
                offer_id=offer_id,
                errors=[error_msg]
            )
    
    def _validate_product(self, product: Dict) -> Optional[PublishResult]:
        """验证产品数据"""
        if not product.get("sku"):
            return PublishResult(
                status=PublishStatus.ERROR,
                message="缺少 SKU"
            )
        
        if not product.get("title") and not product.get("optimization", {}).get("title"):
            return PublishResult(
                status=PublishStatus.ERROR,
                message="缺少产品标题"
            )
        
        price = product.get("suggested_price") or product.get("price", 0)
        if not price or price <= 0:
            return PublishResult(
                status=PublishStatus.ERROR,
                message="价格无效"
            )
        
        return None
    
    def _prepare_publish_data(self, product: Dict) -> Dict:
        """准备发布数据"""
        opt_data = product.get("optimization", {})
        if isinstance(opt_data, str):
            try:
                opt_data = json.loads(opt_data)
            except:
                opt_data = {"title": product.get("title", ""), "description": opt_data}
        
        # 标题处理
        title = opt_data.get("title", product.get("title", ""))[:self.MAX_TITLE_LENGTH]
        
        # 描述处理
        description = opt_data.get("description", product.get("description", "")) or ""
        inventory_description = self._prepare_inventory_description(description)
        
        # 图片处理
        images = self._process_images(product.get("images", []))
        
        # Aspects 处理
        aspects = opt_data.get("aspects", {})
        if not aspects:
            aspects = {"Brand": ["Unbranded"]}
        
        # 价格
        price = product.get("suggested_price") or product.get("price", 0)
        
        return {
            "title": title,
            "description": description,
            "offer_description": description,
            "inventory_description": inventory_description,
            "images": images,
            "aspects": aspects,
            "price": float(price),
            "quantity": resolve_publish_quantity(product["sku"], logger=self.logger),
        }
    
    def _process_images(self, images: Any) -> List[str]:
        """处理图片 URL"""
        from urllib.parse import unquote, urlparse
        
        if isinstance(images, str):
            try:
                images = json.loads(images)
            except:
                images = []
        
        if not isinstance(images, list):
            return []
        
        valid_images = []
        for img in images[:self.MAX_IMAGES]:
            if not isinstance(img, str):
                continue
            
            img = img.strip()
            if not img:
                continue
            
            if not img.startswith(('http://', 'https://')):
                continue
            
            # GigaCloud images need signature params (x-cc, x-cu, x-ct, x-cs) to be accessible
            # Only remove x-oss-process which causes eBay image validation issues
            if '?' in img:
                from urllib.parse import urlparse, parse_qs, urlencode
                parsed = urlparse(img)
                if 'gigab2b.cn' in parsed.netloc:
                    # For GigaCloud: keep signature params, remove only x-oss-process
                    params = parse_qs(parsed.query)
                    if 'x-oss-process' in params:
                        del params['x-oss-process']
                    # Rebuild URL with remaining params
                    new_query = urlencode({k: v[0] for k, v in params.items()})
                    img = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if new_query:
                        img = f"{img}?{new_query}"
                else:
                    # For other sources: remove problematic resize params
                    decoded_url = unquote(img)
                    problematic_params = ['resize', 'width', 'height', 'w=', 'h=']
                    if any(p in decoded_url.lower() for p in problematic_params):
                        img = img.split('?')[0]
            
            img = re.sub(r'/w_\d+,h_\d+[^/]*/', '/', img)
            img = re.sub(r'_\d+x\d+\.', '.', img)
            img = img.replace(' ', '%20').replace('\n', '').replace('\r', '').replace('\t', '')
            
            try:
                parsed = urlparse(img)
                if parsed.scheme and parsed.netloc:
                    valid_images.append(img)
            except:
                pass
        
        return valid_images
    
    def _truncate_description(self, description: str) -> str:
        """截断描述"""
        return self._prepare_inventory_description(description)

    def _compress_html(self, description: str) -> str:
        if not description:
            return ""
        compressed = re.sub(r"<!--.*?-->", "", description, flags=re.DOTALL)
        compressed = re.sub(r">\s+<", "><", compressed)
        compressed = re.sub(r"\s{2,}", " ", compressed)
        return compressed.strip()

    def _prepare_inventory_description(self, description: str) -> str:
        compressed = self._compress_html(description or "")
        if len(compressed) <= self.MAX_DESCRIPTION_LENGTH:
            return compressed
        return smart_truncate_html(
            compressed,
            max_length=self.MAX_DESCRIPTION_LENGTH,
            min_length=max(0, self.MAX_DESCRIPTION_LENGTH - 400),
        )
    
    def _get_category_and_aspects(
        self, 
        title: str, 
        existing_aspects: Dict,
        product: Dict,
        suggested_id: Optional[str] = None
    ) -> Tuple[str, str, Dict[str, List[str]]]:
        """获取类目和完整的 Item Specifics"""
        
        # Collect all known category IDs from our verified maps
        known_ids = set(self.CATEGORY_REQUIRED_ASPECTS.keys())
        for rule in self.CATEGORY_RULES:
            known_ids.add(rule[0])
        
        # 1. 优先使用建议的 ID (来自 AI 优化), but validate it first
        if suggested_id:
            suggested_id = str(suggested_id).strip()
            if suggested_id in known_ids:
                self.logger.info(f"[CATEGORY] Using validated suggested ID: {suggested_id}")
                cat_name = f"Category {suggested_id}"
                aspects = self._ensure_required_aspects(suggested_id, existing_aspects, title, product)
                return suggested_id, cat_name, aspects
            else:
                self.logger.warning(f"[CATEGORY] suggested_id {suggested_id} NOT in known categories, falling through to API/fallback")


        # 2. 尝试使用 API
        try:
            from src.services.ebay_category_matcher import EbayCategoryMatcher
            matcher = EbayCategoryMatcher(self.oauth)
            cat_id, cat_name, aspects = matcher.get_category_and_aspects(title, existing_aspects)
            
            if cat_id:
                aspects = self._ensure_required_aspects(cat_id, aspects, title, product)
                return cat_id, cat_name, aspects
        except Exception as e:
            self.logger.warning(f"[WARN] Category matcher failed: {e}")
        
        # 3. Fallback: 基于关键词匹配
        cat_id, cat_name = self._fallback_category_match(title)
        aspects = self._ensure_required_aspects(cat_id, existing_aspects, title, product)
        
        return cat_id, cat_name, aspects
    
    # Priority-ordered keyword rules for fallback category matching (first match wins)
    # Format: (category_id, category_name, keywords, exclude_patterns)
    CATEGORY_RULES = [
        # ---- Auto Parts & Accessories (match before everything else) ----
        ("262210", "Running Boards & Nerf Bars", [
            "running board", "nerf bar", "side step", "step bar",
        ], ["dog step", "pet step", "baby step"]),
        ("174020", "Trailer Hitches", [
            "trailer hitch", "tow hitch", "hitch receiver", "class 3 hitch", "class 2 hitch",
            "class 4 hitch", "tow receiver",
        ], []),
        ("174021", "Hitch Cargo Carriers", [
            "hitch cargo", "hitch carrier", "hitch basket", "hitch mount cargo",
            "hitch mounted cargo", "folding hitch",
        ], []),
        ("262216", "Roof Racks & Cross Bars", [
            "roof rack", "roof basket", "roof carrier", "rooftop cargo", "cargo basket",
            "cross bar", "crossbar",
        ], ["bookshelf", "shelf", "shoe rack", "wine rack", "coat rack", "towel rack", "baker rack"]),
        ("262093", "Tailgate Parts", [
            "tailgate assist", "tailgate lift", "tailgate ramp", "liftgate ramp",
            "tailgate utility", "gate ramp lift",
        ], []),
        ("85040", "Bike Trailers", [
            "bike trailer", "bicycle trailer", "bike cargo trailer",
        ], []),
        
        # ---- Pet Supplies (match before furniture) ----
        ("121851", "Dog Crates", ["dog crate", "dog kennel", "dog cage"], []),
        ("63108", "Cages, Hutches & Enclosure", ["chicken coop", "chicken house", "poultry coop", "hen house"], []),
        ("63108", "Pet Hutches", ["rabbit hutch", "rabbit cage", "guinea pig"], []),
        ("20740", "Furniture & Scratchers", ["cat tree", "cat tower", "cat condo", "cat house", "catio"], []),
        ("108884", "Dog Houses", ["dog house"], []),
        ("15280", "Treadmills", ["treadmill", "walking pad"], ["pet treadmill"]),
        
        # ---- Pool & Water (before furniture) ----
        ("181068", "Pool Covers", ["pool cover", "pool fence", "pool reel", "pool safety"], []),
        ("145996", "Inflatable Bouncers", [
            "water slide", "bounce house", "inflatable water", "inflatable slide",
            "water park", "bouncy castle", "jumper house", "wet dry splash",
        ], []),
        ("151621", "Barbecues, Grills & Smokers", [
            "bbq grill", "barbecue grill", "propane grill", "gas grill",
            "grill with side burner", "grill griddle", "grill & griddle",
        ], []),
        ("179010", "Tents", [
            "camping tent", "camping tents", "inflatable tent", "inflatable tents",
            "inflatabletent", "glamping tent", "glamping tents", "blow up tent",
            "blow-up tent", "air tent", "suv tent",
        ], []),
        
        # ---- Kids / Bunk Beds (before regular beds) ----
        ("175754", "Bunk Beds", [
            "bunk bed", "loft bed", "triple bed", "triple bunk", "kids bed",
            "toddler bed", "twin over twin", "twin over full", "full over full",
        ], []),
        
        # ---- Sofa Beds (must match before regular beds) ----
        ("38208", "Sofas & Couches", [
            "sofa bed", "pull out sofa", "sleeper sofa", "sleeper couch",
            "convertible sofa", "convertible sleeper",
        ], []),
        
        # ---- Regular Beds ----
        ("175758", "Bed Frames", [
            "bed frame", "platform bed", "upholstered bed", "daybed", "murphy bed",
            "storage bed", "panel bed", "canopy bed", "sleigh bed", "trundle bed",
            "house bed", "floor bed", "montessori bed",
        ], []),
        ("175756", "Headboards", ["headboard", "head board"], ["bed frame"]),
        
        # ---- Mattresses (before Sofas) ----
        ("131588", "Mattresses", ["mattress"], ["mattress topper", "mattress pad", "mattress protector"]),
        ("175751", "Mattress Toppers", ["mattress topper", "mattress pad", "mattress protector"], []),
        
        # ---- Sofas (exclude table items) ----
        ("38208", "Sofas & Couches", [
            "sofa", "couch", "loveseat", "sectional", "futon", "recliner sofa", "sleeper sofa",
            "pull out sofa", "sofa bed", "sleeper couch", "convertible sofa", "convertible sleeper",
            "recliner chair", "push back recliner", "recliner",
        ], ["sofa table", "sofa side table", "sofa end table", "console table", "bean bag"]),
        ("48319", "Bean Bags & Inflatables", ["bean bag"], []),
        
        # ---- Outdoor (before Accent Chairs) ----
        ("79682", "Patio Chairs", [
            "egg chair", "egg swing chair", "hanging egg chair", "hanging swing chair",
            "hanging chair with stand",
            "wicker hanging swing chair", "patio hammock swing chair",
            "hanging basket chair",
        ], []),
        ("79682", "Patio Chairs", [
            "patio chair", "patio chair set", "adirondack", "outdoor chair", "outdoor chair set",
            "outdoor chaise", "outdoor lounge chair", "patio lounge", "rattan chair set",
        ], []),
        ("79686", "Outdoor Tables", ["patio table", "outdoor table"], []),
        ("138996", "Outdoor Daybeds", ["outdoor daybed", "patio daybed", "sunbed"], []),
        ("139849", "Patio & Garden Furniture Sets", ["patio furniture", "patio set", "outdoor sofa", "outdoor sectional"], []),
        ("180997", "Shade Sails", ["shade sail", "sun shade sail", "sunshade sail"], []),
        
        # ---- Kitchen Islands (before Tables/Cabinets) ----
        ("177000", "Kitchen Islands", ["kitchen island", "kitchen cart", "baker rack", "rolling kitchen"], []),
        ("115753", "Kitchen Carts", ["microwave cart", "microwave stand", "utility cart"], []),
        
        # ---- Dining ----
        ("107578", "Dining Sets", [
            "dining set", "dining table set", "table set with", "table and chair",
            "table with chair", "pub set", "table set for",
        ], []),
        ("54235", "Chairs", ["dining chair", "kitchen chair", "counter chair", "parsons chair"], ["desk chair", "office chair"]),
        ("103431", "Bar Stools & Stools", ["bar stool", "counter stool", "barstool"], []),
        ("38204", "Bar Tables", ["bar table", "pub table", "counter table", "counter height table"], []),
        
        # ---- Tables ----
        ("38204", "Coffee Tables", ["coffee table"], []),
        ("38204", "Tables", ["board game table", "gaming table", "game table"], []),
        ("38205", "Console Tables", ["console table", "sofa table", "entry table", "hall table", "entryway table"], []),
        ("38200", "End Tables", ["end table", "side table", "accent table", "nightstand table"], []),
        ("38199", "Nightstands", ["nightstand", "night stand", "bedside table"], []),
        ("38204", "Dining Tables", ["dining table"], ["set", "chair", "kitchen island", "stool"]),
        
        # ---- Bathroom (before desks) ----
        ("32878", "Bathroom Vanities", [
            "bathroom vanity", "bath vanity", "sink vanity", "vanity sink", "vanity with sink",
        ], []),
        
        # ---- Office ----
        ("88057", "Desks", [
            "desk", "computer desk", "writing desk", "standing desk", "l-shaped desk",
            "corner desk", "executive desk", "office desk", "vanity desk",
        ], ["daybed", "bed frame", "bedside", "desk chair", "desk lamp", "nightstand", "kitchen island"]),
        ("54235", "Office Chairs", [
            "office chair", "desk chair", "executive chair", "ergonomic chair",
            "task chair", "gaming chair",
        ], []),
        
        # ---- Sideboards (before Cabinets) ----
        ("183322", "Sideboards & Buffets", ["buffet", "sideboard", "credenza"], []),
        ("45331", "Wine Racks", ["wine rack", "wine cabinet", "bar cabinet", "liquor cabinet"], []),
        ("20488", "TV Stands", ["tv stand", "entertainment center", "media console", "tv cabinet"], []),
        
        # ---- Cabinets ----
        ("20487", "Cabinets", [
            "cabinet", "pantry", "cupboard", "storage cabinet", "accent cabinet",
            "floor cabinet", "linen cabinet", "bathroom cabinet", "medicine cabinet",
        ], ["tv cabinet", "file cabinet", "china cabinet", "bar cabinet", "wine cabinet", "sideboard"]),
        ("3199", "Bookcases", ["bookshelf", "bookcase", "book shelf", "etagere", "ladder shelf"], []),
        ("20493", "Display Cabinets", ["display cabinet", "curio cabinet", "glass cabinet"], []),
        ("103430", "Armoires & Wardrobes", ["wardrobe", "armoire", "jewelry armoire"], []),
        
        # ---- Chairs (generic, after specific types) ----
        ("54235", "Accent Chairs", [
            "accent chair", "arm chair", "lounge chair", "club chair", "barrel chair",
            "papasan", "chaise lounge", "wingback chair",
        ], ["outdoor", "patio", "desk chair", "office chair", "dining chair"]),
        ("20877", "Rocking Chairs", ["rocking chair", "glider"], []),
        
        # ---- Misc Furniture ----
        ("38221", "Shoe Storage", ["shoe rack", "shoe cabinet", "shoe bench", "shoe storage"], []),
        ("20580", "Mirrors", ["wall mirror", "floor mirror", "full length mirror", "vanity mirror"], []),
        ("114397", "Dressers & Chests of Drawers", ["dresser", "chest of drawer"], []),
        ("175755", "Clothes Racks", ["clothes rack", "garment rack"], []),
        
        # ---- Sports & Fitness ----
        ("58102", "Exercise Bikes", ["exercise bike", "stationary bike", "spin bike"], []),
        ("57275", "Trampolines", ["trampoline"], []),
        
        # ---- Luggage ----
        ("16289", "Suitcases", ["suitcase", "luggage"], []),
        
        # ---- Other ----
        ("175764", "Room Dividers", ["room divider", "partition", "screen divider"], []),
        ("261263", "Hall Trees & Stands", ["hall tree"], []),
        ("262980", "Benches", ["storage bench", "entryway bench", "shoe bench"], []),
        ("112581", "Lamps", ["floor lamp", "standing lamp", "crystal lamp"], []),
        ("50876", "Sports Nets", ["golf net", "batting cage", "practice net"], []),
        ("175761", "Ottomans", ["ottoman", "footstool", "pouf"], []),
    ]
    
    def _fallback_category_match(self, title: str) -> Tuple[str, str]:
        """基于优先级关键词的类目匹配 (first match wins)"""
        title_lower = title.lower()
        
        for cat_id, cat_name, keywords, exclude_patterns in self.CATEGORY_RULES:
            # Check exclusion patterns first
            if any(exc in title_lower for exc in exclude_patterns):
                continue
            if any(kw in title_lower for kw in keywords):
                return cat_id, cat_name
        
        # Legacy fallback: try CATEGORY_KEYWORDS dict
        for cat_id, keywords in self.CATEGORY_KEYWORDS.items():
            if any(kw in title_lower for kw in keywords):
                return cat_id, f"Category {cat_id}"
        
        return "38208", "Sofas, Armchairs & Couches"
    
    def _ensure_required_aspects(
        self, 
        category_id: str, 
        aspects: Dict, 
        title: str,
        product: Dict
    ) -> Dict[str, List[str]]:
        """确保所有必填 Item Specifics 都存在"""
        completed = {}
        for k, v in (aspects or {}).items():
            if isinstance(v, str):
                value = v.strip()
                if value:
                    completed[k] = [value]
            elif isinstance(v, list):
                values = [str(i).strip() for i in v if str(i).strip()]
                if values:
                    completed[k] = values
            elif v is not None:
                value = str(v).strip()
                if value:
                    completed[k] = [value]

        sanitize_placeholder_aspects(completed, title=title, category_id=category_id)
        sanitize_single_value_aspects(completed)
        
        cat_config = self.CATEGORY_REQUIRED_ASPECTS.get(category_id, {})
        required = cat_config.get("required", ["Brand"])
        defaults = cat_config.get("defaults", {"Brand": "Unbranded"})

        attrs = product.get("attributes", {}) if isinstance(product, dict) else {}
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except Exception:
                attrs = {}

        description = product.get("description", "") if isinstance(product, dict) else ""
        completed = complete_publish_aspects(
            completed,
            title=title,
            category_id=category_id,
            attrs=attrs,
            description=description,
            category_required_aspects=self.CATEGORY_REQUIRED_ASPECTS,
            log=lambda message: self.logger.info(message),
            ensure_required_dimensions=False,
            fill_item_weight=False,
        )
        
        title_lower = title.lower()
        
        for aspect_name in required:
            if aspect_name in completed and completed[aspect_name]:
                continue
            
            detected = self._smart_detect_aspect(aspect_name, title_lower, product, completed)
            
            if detected:
                completed[aspect_name] = [detected]
            else:
                default_val = defaults.get(aspect_name, "Unbranded")
                completed[aspect_name] = [default_val] if isinstance(default_val, str) else default_val

        sanitize_placeholder_aspects(completed, title=title, category_id=category_id)
        sanitize_single_value_aspects(completed)
        enforce_store_brand_aspect(completed)
        
        return completed
    
    def _smart_detect_aspect(
        self, 
        aspect_name: str, 
        title: str, 
        product: Dict,
        existing: Dict
    ) -> Optional[str]:
        """智能检测 Aspect 值"""
        detectors = {
            "Upholstery Fabric": self._detect_fabric,
            "Color": self._detect_color,
            "Material": self._detect_material,
            "Type": self._detect_type,
            "Compatible Mattress Size": self._detect_bed_size,
            "Item Width": self._detect_dimension,
            "Item Height": self._detect_dimension,
            "Item Length": self._detect_dimension,
            "Brand": lambda *args: "Unbranded",
            "Set Includes": self._detect_set_includes,
        }
        
        detector = detectors.get(aspect_name)
        if detector:
            return detector(title, product, existing, aspect_name)
        return None
    
    def _detect_fabric(self, title: str, product: Dict, existing: Dict, *args) -> Optional[str]:
        """检测面料类型"""
        return infer_upholstery_fabric(title, existing)
    
    def _detect_color(self, title: str, product: Dict, existing: Dict, *args) -> Optional[str]:
        """检测颜色"""
        if existing.get("Color"): return existing["Color"][0]
        colors = {"red": "Red", "blue": "Blue", "green": "Green", "black": "Black", "white": "White", "gray": "Gray"}
        for k, v in colors.items():
            if k in title: return v
        return "Gray"
    
    def _detect_material(self, title: str, product: Dict, existing: Dict, *args) -> Optional[str]:
        """检测材质"""
        if existing.get("Material"): return existing["Material"][0]
        materials = {"wood": "Wood", "metal": "Metal", "plastic": "Plastic", "fabric": "Fabric"}
        for k, v in materials.items():
            if k in title: return v
        return "Mixed Materials"
    
    def _detect_type(self, title: str, product: Dict, existing: Dict, *args) -> Optional[str]:
        """检测产品类型"""
        if existing.get("Type"): return existing["Type"][0]
        types = {"sofa": "Sofa", "bed": "Bed", "table": "Table", "chair": "Chair", "mattress": "Mattress"}
        for k, v in types.items():
            if k in title: return v
        return None
    
    def _detect_bed_size(self, title: str, product: Dict, existing: Dict, *args) -> Optional[str]:
        """检测床垫尺寸"""
        category_id = ""
        if isinstance(product, dict):
            category_id = str(product.get("categoryId") or product.get("category_id") or "")
            if not category_id:
                opt = product.get("optimization", {})
                if isinstance(opt, str):
                    try:
                        opt = json.loads(opt)
                    except Exception:
                        opt = {}
                if isinstance(opt, dict):
                    category_id = str(opt.get("categoryId") or "")
        return infer_bed_size(title, category_id)
    
    def _detect_dimension(self, title: str, product: Dict, existing: Dict, aspect_name: str) -> Optional[str]:
        """检测尺寸 — 从specs/attributes/optimization提取，无数据时不虚构"""
        import re as _re
        
        # 尝试从产品规格中提取
        specs = product.get("specs", {})
        if isinstance(specs, str):
            try:
                specs = json.loads(specs)
            except:
                specs = {}
        
        attrs = product.get("attributes", {})
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except:
                attrs = {}
        
        # Also check optimization dimensions
        opt = product.get("optimization", {})
        if isinstance(opt, str):
            try:
                opt = json.loads(opt)
            except:
                opt = {}
        opt_dims = opt.get("dimensions", {})
        
        # 映射 aspect 名称到可能的 spec/attr 键
        dim_map = {
            "Item Width": ["width", "Width", "assembled width", "Assembled Width", "Assembled Width (in.)", "product width", "宽", "W"],
            "Item Height": ["height", "Height", "assembled height", "Assembled Height", "Assembled Height (in.)", "product height", "高", "H"],
            "Item Length": ["length", "Length", "assembled length", "Assembled Length", "Assembled Length (in.)", "depth", "Depth", "product length", "长", "深", "L", "D"],
        }
        
        # Map aspect name to optimization dimension key
        opt_dim_map = {
            "Item Width": "width",
            "Item Height": "height",
            "Item Length": "length",
        }
        
        possible_keys = dim_map.get(aspect_name, [])
        
        # Check specs first
        for key in possible_keys:
            if key in specs:
                val = specs[key]
                if isinstance(val, (int, float)) and val > 0:
                    return f"{val} in"
                val_str = str(val).strip()
                if val_str and _re.search(r'\d', val_str):
                    if "in" not in val_str.lower() and "cm" not in val_str.lower():
                        return f"{val_str} in"
                    return val_str
        
        # Check attributes
        for key in possible_keys:
            if key in attrs:
                val = attrs[key]
                if isinstance(val, (int, float)) and val > 0:
                    return f"{val} in"
                val_str = str(val).strip()
                if val_str and _re.search(r'\d', val_str):
                    if "in" not in val_str.lower() and "cm" not in val_str.lower():
                        return f"{val_str} in"
                    return val_str
        
        # Check optimization dimensions
        opt_key = opt_dim_map.get(aspect_name)
        if opt_key and opt_dims.get(opt_key):
            val = opt_dims[opt_key]
            if isinstance(val, (int, float)) and val > 0:
                return f"{val} in"
            val_str = str(val).strip()
            if val_str and _re.search(r'\d', val_str):
                return val_str
        
        # Try to extract from title (e.g., "60x20x6" or "60\"x20\"")
        dim_match = _re.search(r'(\d+\.?\d*)\s*[x×X]\s*(\d+\.?\d*)\s*[x×X]\s*(\d+\.?\d*)', title)
        if dim_match:
            l, w, h = float(dim_match.group(1)), float(dim_match.group(2)), float(dim_match.group(3))
            if aspect_name == "Item Length":
                return f"{l} in"
            elif aspect_name == "Item Width":
                return f"{w} in"
            elif aspect_name == "Item Height":
                return f"{h} in"
        
        # No data found — do NOT hallucinate fake dimensions
        # Return "Refer to Product Images" instead of arbitrary defaults
        return "Refer to Product Images"
    
    def _detect_set_includes(self, title: str, product: Dict, existing: Dict, *args) -> Optional[str]:
        """检测 Set Includes（套装包含的组件）"""
        # Try to get from existing aspects first
        if existing.get("Set Includes"):
            val = existing["Set Includes"]
            return val[0] if isinstance(val, list) else val

        detected = infer_set_includes(title)
        if detected:
            return detected

        title_lower = title.lower()
        if "dining" in title_lower:
            if "chair" in title_lower:
                return "Dining Table & Chairs"
            if "stool" in title_lower:
                return "Dining Table & Stools"
            return "Dining Table & Chairs"
        if "ottoman" in title_lower and ("sofa" in title_lower or "sectional" in title_lower):
            return "Sofa & Ottoman"
        if "table" in title_lower:
            return "Table"
        return None

    def _create_inventory_item(self, sku: str, data: Dict) -> Dict:
        """创建或更新 Inventory Item"""
        return self.ebay_client.create_or_replace_inventory_item(
            sku=sku,
            product={
                "title": data["title"],
                "description": data.get("inventory_description", data["description"]),
                # MAPPING CRITICAL: internal 'images' -> client 'image_urls'
                "image_urls": data["images"],
                "price": data["price"],
                "quantity": data.get("quantity", 1),
                "condition": "NEW",
                "aspects": data["aspects"]
            }
        )

    def _prepare_motors_compatibility(
        self,
        category_id: str,
        title: str,
        description: str,
        aspects: Dict,
    ) -> CompatibilityAnalysis:
        compatibility = analyze_ebay_motors_compatibility(category_id, title, description, aspects)
        if compatibility.mode != "not_applicable":
            self.logger.info(f"[COMPAT] {compatibility.summary}")
            for issue in compatibility.issues:
                self.logger.warning(f"[COMPAT] {issue}")
        return compatibility

    def _sync_inventory_compatibility(self, sku: str, compatibility: CompatibilityAnalysis) -> None:
        if compatibility.mode == "not_applicable":
            return
        if compatibility.compatible_products:
            self.ebay_client.create_or_replace_product_compatibility(sku, compatibility.compatible_products)
        else:
            self.ebay_client.delete_product_compatibility(sku)
    
    def _create_offer(self, sku: str, price: float, category_id: str, listing_description: str) -> Dict:
        """创建 Offer, auto-detect EBAY_MOTORS marketplace for auto parts categories"""
        marketplace_id = "EBAY_MOTORS" if category_id in self.EBAY_MOTORS_CATEGORIES else None
        if marketplace_id:
            self.logger.info(f"[CATEGORY] Using EBAY_MOTORS marketplace for category {category_id}")
        return self.ebay_client.create_offer(
            sku,
            price,
            category_id,
            listing_description=listing_description,
            marketplace_id=marketplace_id,
        )
    
    def _publish_offer(self, offer_id: str) -> Dict:
        """发布 Offer"""
        return self.ebay_client.publish_offer(offer_id)
    
    def _parse_error_message(self, error: str) -> str:
        """解析错误消息"""
        if "category" in error.lower() and "invalid" in error.lower():
            return "类目无效，请检查产品分类"
        return error[:200]

def create_publisher(environment: str = "PRODUCTION") -> EbayPublisher:
    oauth = EbayOAuthService(environment)
    return EbayPublisher(oauth)
