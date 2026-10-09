from scripts.auto_rotate_promotions import format_promotion_created_subject


def test_promotion_created_subject_includes_store_brand():
    subject = format_promotion_created_subject("AquaRides")
    assert subject == "🏷️ AquaRides 店铺折扣新一期 - 5% off 2天"
