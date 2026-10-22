"""Motors category: reject implausible Taxonomy leaves for hitch carriers."""
from unittest.mock import MagicMock, patch

from src.services.ebay_category_matcher import EbayCategoryMatcher


def test_motors_implausible_api_falls_back_to_tree100_hitch():
    matcher = EbayCategoryMatcher(oauth_service=MagicMock())
    title = (
        '800LBS Motorcycle Trailer Hitch Scooter Hauler Mount Rack 2" '
        "Motorcycle Tow Carrier Receiver Heavy Duty Steel"
    )
    with patch("src.services.ebay_category_matcher.get_store_profile") as gp:
        profile = MagicMock()
        profile.is_motors = True
        profile.category_tree_id = "100"
        gp.return_value = profile
        with patch.object(
            matcher,
            "_fallback_category",
            return_value=("174021", "Hitch Cargo Carriers"),
        ):
            with patch.object(
                matcher,
                "canonicalize_category",
                side_effect=lambda title, cid, name=None, description=None: (cid, name),
            ):
                with patch.object(
                    matcher.oauth,
                    "get_application_token",
                    return_value="tok",
                ):
                    with patch("src.services.ebay_category_matcher.requests.get") as get:
                        get.return_value.status_code = 200
                        get.return_value.json.return_value = {
                            "categorySuggestions": [
                                {
                                    "category": {
                                        "categoryId": "184478",
                                        "categoryName": "Motorcycle Accessories",
                                    }
                                }
                            ]
                        }
                        cid, name = matcher._get_best_category(title)
    assert cid == "33653"
    assert name == "Trailer Hitches"
