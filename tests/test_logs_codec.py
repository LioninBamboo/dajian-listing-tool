"""Fixture-shape coverage for the shared collected_products.logs codec."""

from __future__ import annotations

import json

import pytest

from src.utils.logs_codec import ParsedLogs, append_log, dump_logs, parse_logs

VALID = '["Received from extension", "Auto-analyzed at 2026-09-15T04:00:21.425901"]'
TRAILING_SHORT = (
    '["Listing: 366407614496, Offer: 108006576011, Cat: 38204"]\n'
    "[2026-10-08 11:26:48] LONG_OOS_END_LIVE withdraw 108006576011=OK"
)
TRAILING_XML = (
    '["Category ID: 38208"]\n'
    '[2026-10-10 08:35:14] CRO_END_LIVE_20261010 <?xml version="1.0" encoding="UTF-8"?>\n'
    '<EndFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
    "<Ack>Success</Ack></EndFixedPriceItemResponse>"
)
TRAILING_DICT = (
    '[{"event": "semantic_rewrite", "sku": "W757P281147"}]\n'
    "[2026-09-27 08:53:21] OOS_NO_ARRIVAL_DELIST withdraw 107528520011=OK"
)
EMPTY = ""
NULL_JSON = "null"
SCALAR = '"just a string"'
DICT = '{"a": 1}'
GARBAGE = "not json at all {"

NEW_ENTRY = "normalized-at-test"


def test_parse_none_and_whitespace_are_empty():
    for raw in (None, "", "  \n\t"):
        parsed = parse_logs(raw)
        assert parsed.status == "empty"
        assert parsed.entries == []
        assert parsed.trailing == ""
        assert parsed.entries_with_trailing() == []


def test_parse_already_decoded_list_is_ok():
    parsed = parse_logs(["Received from extension", "done"])
    assert parsed.status == "ok"
    assert parsed.entries == ["Received from extension", "done"]
    assert parsed.trailing == ""


def test_parse_already_decoded_dict_is_non_list():
    parsed = parse_logs({"a": 1})
    assert parsed.status == "non_list"
    assert parsed.entries == [{"a": 1}]


def test_parse_valid_json_list():
    parsed = parse_logs(VALID)
    assert parsed.status == "ok"
    assert parsed.entries == json.loads(VALID)
    assert parsed.trailing == ""


def test_parse_null_scalar_and_dict_strings():
    null = parse_logs(NULL_JSON)
    assert null.status == "non_list"
    assert null.entries == []

    scalar = parse_logs(SCALAR)
    assert scalar.status == "non_list"
    assert scalar.entries == ["just a string"]

    obj = parse_logs(DICT)
    assert obj.status == "non_list"
    assert obj.entries == [{"a": 1}]


@pytest.mark.parametrize(
    ("raw", "leading"),
    [
        (TRAILING_SHORT, ["Listing: 366407614496, Offer: 108006576011, Cat: 38204"]),
        (TRAILING_XML, ["Category ID: 38208"]),
        (TRAILING_DICT, [{"event": "semantic_rewrite", "sku": "W757P281147"}]),
    ],
)
def test_parse_trailing_text_keeps_tail(raw, leading):
    parsed = parse_logs(raw)
    assert parsed.status == "trailing_text"
    assert parsed.entries == leading
    assert parsed.trailing == raw.split("\n", 1)[1]
    assert parsed.entries_with_trailing() == leading + [parsed.trailing]


def test_trailing_xml_is_byte_identical():
    parsed = parse_logs(TRAILING_XML)
    trailing = parsed.trailing
    assert "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" in trailing
    assert "<Ack>Success</Ack>" in trailing
    assert "\n<EndFixedPriceItemResponse" in trailing
    combined = parsed.entries_with_trailing()
    assert json.loads(dump_logs(combined)) == combined
    assert combined[-1] == trailing
    assert trailing.encode("utf-8") == (
        b'[2026-10-10 08:35:14] CRO_END_LIVE_20261010 '
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<EndFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        b"<Ack>Success</Ack></EndFixedPriceItemResponse>"
    )


def test_parse_garbage_keeps_raw():
    parsed = parse_logs(GARBAGE)
    assert parsed.status == "garbage"
    assert parsed.entries == []
    assert parsed.trailing == GARBAGE
    assert parsed.entries_with_trailing() == [GARBAGE]


def test_dump_logs_rejects_non_list():
    with pytest.raises(TypeError):
        dump_logs({"a": 1})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw",
    [VALID, TRAILING_SHORT, TRAILING_XML, TRAILING_DICT, EMPTY, None, NULL_JSON, SCALAR, DICT, GARBAGE],
)
def test_append_log_is_valid_json_and_keeps_history(raw):
    out = append_log(raw, NEW_ENTRY)
    loaded = json.loads(out)
    assert isinstance(loaded, list)
    assert loaded[-1] == NEW_ENTRY
    previous = parse_logs(raw).entries_with_trailing()
    assert loaded[:-1] == previous


def test_parsed_logs_round_trip_via_dump():
    parsed = parse_logs(TRAILING_XML)
    payload = parsed.entries_with_trailing()
    assert json.loads(dump_logs(payload)) == payload
    assert isinstance(parsed, ParsedLogs)
