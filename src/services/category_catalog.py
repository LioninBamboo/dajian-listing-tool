"""
Shared eBay category catalog (id ↔ display name ↔ search keywords).

Single source of truth for category metadata used by:
- src/web/pages/competition_monitor.py
- scripts that emit category-level reports

Adding new categories: keep keys unique. The previous inline copy in
competition_monitor.py had duplicate keys (100411, 63108) which silently
overwrote the earlier entry — see CATEGORY_NAMES_DUPLICATES_FIXED below.
"""
from __future__ import annotations

# eBay leaf category id → English display name
CATEGORY_NAMES: dict[str, str] = {
    '175758': 'Bed Frames',
    '38200': 'End Tables',
    '38204': 'Dining Tables',
    '20488': 'TV Stands',
    '38208': 'Sofas',
    '175754': 'Kids Beds',
    '118218': 'Accent Chairs',
    '63108': 'Pet Hutches',  # was duplicated as 'Chicken Coops & Hutches'
    '20580': 'Bathroom Vanities',
    '175750': 'Smart Toilets',
    '38205': 'Console Tables',
    '25458': 'Dining Chairs',
    '88057': 'Vanity Desks',
    '63557': 'Sideboards',
    '16080': 'Luggage Sets',
    '131588': 'Mattresses (King)',
    '20487': 'Storage Cabinets',
    '177816': 'Dining Tables (Metal)',
    '100411': 'Cat Litter Furniture',  # was duplicated as 'Cat Litter Boxes'
    '20744': 'Dog Beds',
    '183316': 'Kitchen Islands (Seating)',
    '177000': 'Kitchen Islands (Power)',
    '68240': 'Wardrobe Cabinets',
    '20493': 'Display Cabinets',
    '175761': 'Ottomans',
    '3199': 'Bookshelves',
    '175756': 'Headboards',
    '42428': 'Medicine Cabinets',
    '32878': 'Bath Vanities+Sink',
    '20466': 'Dressers',
    '121851': 'Dog Crates',
    '103431': 'Bar Stools & Stools',
    '20740': 'Cat Trees',
    '20490': 'Storage Ottomans',
    '57275': 'Trampolines',
    '106198': 'Mattresses (Queen)',
    '15280': 'Treadmills',
    '158916': 'Weight Benches',
    '20877': 'Glider Chairs',
    '116380': 'Pet Strollers',
    '177788': 'Pet Carriers',
    '38221': 'Hall Trees',
    '22513': 'Gaming Chairs',
    '181270': 'Gaming Chairs (Ergo)',
    '54235': 'Office Chairs',
    '45331': 'Wine Racks',
    '38217': 'Wine Cabinets',
    '66763': 'Storage Benches',
    '66758': 'Kids Tables',
    '38199': 'Nightstands',
    # New categories added from audit
    '25863': 'Patio Sets',
    '79686': 'Outdoor Tables',
    '85916': 'Fire Pits',
    '181000': 'Fire Pits (Legacy)',
    '79684': 'Patio Tables',
    '20518': 'Planters & Pots',
    '29511': 'Ornaments & Statues',
    '29514': 'Plant Stands',
    '181087': 'Coolers',
    '79682': 'Patio Furniture',
    '159128': 'Table Tennis',
    '20608': 'Kitchen Fixtures',
    '75671': 'Wheelbarrows & Wagons',
    '106917': 'Luggage (Carry-On)',
    '131604': 'Tub & Shower Doors',
    '115753': 'Bathroom Vanity Tops',
    '50876': 'Outdoor Dining Tables',
    '30889': 'Desks',
    '107578': 'Dining Sets',
    '22656': 'Coat & Hat Racks',
    '115625': 'Bathroom Sinks',
    '257900': 'Office Furniture',
    '175751': 'Platform Beds',
    '80177': 'Carry-On Luggage',
    '112581': 'Lamps',
    '138996': 'Outdoor Daybeds',
    '139946': 'Fence Panels',
    '261263': 'Hall Trees & Stands',
    '262980': 'Benches',
}

# eBay leaf category id → space-separated keywords used for live market lookup.
# Kept separate from CATEGORY_NAMES because the same id may have multiple
# product variants worth searching independently.
CATEGORY_KEYWORDS: dict[str, str] = {
    '175758': 'accent chair chaise lounge indoor',
    '38200':  'kitchen island with storage cabinet',
    '38204':  'counter height dining table set',
    '20488':  'TV stand entertainment center modern',
    '38208':  'sectional sofa couch living room',
    '175754': 'kids bed frame with storage',
    '118218': 'swivel lounge chair accent',
    '63108':  'large chicken coop outdoor wooden',
    '20580':  'bathroom vanity cabinet with mirror',
    '175750': 'smart toilet bidet tankless',
    '38205':  'console table entryway with storage',
    '25458':  'dining chair set upholstered',
    '88057':  'makeup vanity desk with mirror drawers',
    '63557':  'sideboard buffet cabinet modern',
    '16080':  'luggage set hardshell spinner',
    '131588': 'king size mattress memory foam',
    '20487':  'storage cabinet with doors',
    '177816': 'modern dining chair set metal',
    '100411': 'cat litter box enclosure furniture',
    '20744':  'human dog bed large sofa',
    '183316': 'kitchen island with seating',
    '177000': 'kitchen island drop leaf with power',
    '68240':  'metal wardrobe cabinet storage',
    '20493':  'glass display cabinet curio',
    '175761': 'ottoman storage furniture',
    '3199':   'corner bookshelf tall display',
    '175756': 'bed frame with storage LED',
    '42428':  'medicine cabinet with mirror',
    '32878':  'bathroom vanity with sink',
    '20466':  'dresser with drawers modern',
    '121851': 'large dog crate furniture',
    '103431': 'entryway shoe bench storage',
    '20740':  'cat tree with litter box',
    '20490':  'storage ottoman with lift top',
    '57275':  'mini trampoline rebounder',
    '106198': 'queen mattress memory foam',
    '15280':  'folding treadmill for home',
    '158916': 'kitchen island with trash can',
    '20877':  'nursery glider rocking chair',
    '177788': 'pet carrier airline approved',
    '116380': 'pet stroller foldable travel',
    '38221':  'hall tree with storage bench',
    '22513':  'gaming chair with footrest',
    '181270': 'ergonomic gaming chair massage',
    '54235':  'ergonomic office chair mesh',
    '45331':  'wall mounted wine rack',
    '38217':  'wine bar cabinet with hutch',
    '66763':  'L-shaped storage bench corner',
    '66758':  'kids table and chair set',
    '38199':  'bedside table nightstand vanity',
}


def category_name(cat_id: str, default: str | None = None) -> str:
    """Lookup display name; returns the id itself if unknown and no default."""
    if not cat_id:
        return default or ''
    return CATEGORY_NAMES.get(str(cat_id), default if default is not None else str(cat_id))
