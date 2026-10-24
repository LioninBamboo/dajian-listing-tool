"""Apply-mode --limit / --max-reads / rotation for scripts/fitment_audit.py.

Trading reads are mocked. These tests never call live eBay.

The AquaRides SQLite snapshot is not in the repo. Titles, descriptions, and
optimization JSON below are reconstructed so classify_fitment and
resolve_apply_fitment reproduce the 2026-10-24 gate table (skip reason or
stored/title-built entry count). Position fillers are marked SYNTHETIC.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from src.services.fitment_qc import build_compatible_products
from src.utils.store_profile import StoreProfile, reset_store_profile_cache

AUDIT_REPORT_KEYS = {
    "generated_at",
    "mode",
    "vehicle_specific",
    "universal",
    "listings_with_issues",
    "missing",
    "mismatch",
    "applied",
    "apply_fail",
    "verified_ok",
    "verify_failed",
    "skipped_no_source_fitment",
    "skipped_title_no_year",
    "read_failed",
    "findings",
}
APPLY_EXTRA_KEYS = {
    "limit",
    "max_reads",
    "reads_used",
    "candidates",
    "has_fitment",
    "read_cap_hit",
    "state_file",
}


@pytest.fixture(autouse=True)
def _reset_profile_cache():
    reset_store_profile_cache()
    yield
    reset_store_profile_cache()


def _aqua_auto_profile() -> StoreProfile:
    return StoreProfile(
        store_kind="auto",
        brand_name="AquaRides",
        listing_channel="trading",
        ebay_site_id="100",
        qc_profile="motors",
        scheduler_profile="ops",
    )


def ymm(make: str, model: str, start: int, end: int) -> list[dict]:
    return [
        {
            "compatibilityProperties": [
                {"name": "Year", "value": str(year)},
                {"name": "Make", "value": make},
                {"name": "Model", "value": model},
            ]
        }
        for year in range(start, end + 1)
    ]


def _opt(stored: list[dict] | None) -> dict:
    if not stored:
        return {}
    return {"motorsCompatibility": {"compatibleProducts": stored}}


class Case:
    def __init__(
        self,
        case_id,
        sku,
        item_id,
        db_pos,
        title,
        *,
        description="AquaRides hardware kit.",
        stored=None,
        live="missing",
        counts=True,
        reads=1,
        gate="write",
        write_n=0,
        live_entries=None,
    ):
        self.case_id = case_id
        self.sku = sku
        self.item_id = str(item_id)
        self.db_pos = db_pos
        self.title = title
        self.description = description
        self.stored = list(stored or [])
        self.live = live
        self.counts = counts
        self.reads = reads
        self.gate = gate
        self.write_n = write_n
        self.live_entries = list(live_entries or [])

    def row(self, row_id=None):
        return {
            "id": self.db_pos if row_id is None else row_id,
            "sku": self.sku,
            "listing_id": self.item_id,
            "title": self.title,
            "description": self.description,
            "optimization": _opt(self.stored),
        }


def _cases():
    defender = "Off-Road Tailgate Flagpole Mount Kit Fit For New Land Rover Defender 90/110"
    wrangler_both = "Running Boards for 2007-2026 Jeep Wrangler JK/JL, Gladiator JT"
    wrangler_jk = "Side Steps for 2007-2026 Jeep Wrangler JK JL"
    wrangler_jl = "Nerf Bars for 2018-2025 Jeep Wrangler JL"
    silverado_sierra = (
        "Running Boards for 2019-2026 Chevy Silverado 1500, 2020-2026 GMC Sierra 1500"
    )
    gladiator_2020 = "Steps for 2020-2026 Jeep Gladiator JT"
    tacoma_2005 = "Running Boards Side Steps Fit Toyota Tacoma 2005-2023 (Crew Cab)"
    silverado_noyear = "Running Boards for Chevy Silverado 1500 / GMC Sierra 1500"
    wrangler_0718 = "Nerf Bars For 07-18 Jeep Wrangler JK"
    tacoma_2024 = "4PCS Running Boards For 2024 2025 Toyota Tacoma Double Cab/Crew Cab Side Steps"
    bronco = "Running Boards For 2021+ Ford Bronco"
    f150_2015 = "Bed Rails for 2015-2026 Ford F-150"
    gladiator_2018 = "Steps for 2018-2026 Jeep Gladiator JT"
    ford_trucks = "Boards for 2015-2026 Ford F150, 2017-2025 F250/F350"
    tacoma_2016 = "Boards for 2016-2023 Toyota Tacoma"
    cart = "Door Storage Cart for 2007-2026 Jeep Wrangler JK/JL, Gladiator"
    silverado_old = "Trailer Hitch for 1999-2013 Chevrolet Silverado 1500 / GMC Sierra 1500"
    sienna = "Class 3 Hitch Compatible with 2004-2020 Toyota Sienna All Styles"
    highlander = "Rear Trailer Hitch Compatible For Toyota Highlander 2008-2013"
    mdx_pilot = "Trailer Hitch for 2014-2020 Acura MDX / Honda Pilot"
    pilot = "Trailer Hitch for 2003-2008 Honda Pilot"
    traction_jl = "Off-Road Recovery Traction Boards for 2018-2024 Jeep Wrangler JL"
    traction_jt = "Off-Road Traction Boards for 2020-2026 Jeep Gladiator JT"
    rav4 = "Trailer Hitch for 2006-2018 Toyota RAV4"
    return [
        Case("1", "W2801P540956", "188777280873", 5, defender, description="Tailgate flagpole mount kit. Hardware included.", live="missing", counts=True, reads=1, gate="skipped_no_source_fitment"),
        Case("2", "W2801P516393", "188777275513", 17, wrangler_both, stored=ymm("Jeep", "Wrangler", 2007, 2026) + ymm("Jeep", "Gladiator", 2007, 2026), gate="write", write_n=40),
        Case("3", "W3611P511722", "188777336556", 31, wrangler_jk, stored=ymm("Jeep", "Wrangler", 2007, 2026), gate="write", write_n=20),
        Case("4", "W2801P516391", "188777415130", 32, wrangler_jl, description="Steel nerf bar kit with mounting hardware.", gate="skipped_no_source_fitment"),
        Case("5", "W3611P453316", "188777285647", 33, silverado_sierra, stored=ymm("Chevrolet", "Silverado 1500", 2019, 2026) + ymm("GMC", "Sierra 1500", 2019, 2026), gate="write", write_n=16),
        Case("6", "W3611P453319", "188843785469", 34, gladiator_2020, stored=ymm("Jeep", "Gladiator", 2020, 2026), gate="write", write_n=7),
        Case("7", "W3611P511714", "188777332996", 36, tacoma_2005, description=tacoma_2005, gate="write", write_n=19),
        Case("8", "W3611P511712", "188777327290", 37, silverado_noyear, stored=ymm("Chevrolet", "Silverado 1500", 2015, 2018), gate="skipped_title_no_year"),
        Case("9", "W3611P339830", "188797217206", 42, wrangler_0718, stored=ymm("Jeep", "Wrangler", 2007, 2018), gate="skipped_title_no_year"),
        Case("10", "W3611P339821", "188804151335", 91, tacoma_2024, description=tacoma_2024, stored=ymm("Toyota", "Tacoma", 2005, 2025), gate="skipped_stored_mismatch"),
        Case("11", "W2801P298871", "188804154545", 92, bronco, stored=ymm("Ford", "Bronco", 2021, 2026), gate="write", write_n=6),
        Case("12", "W3611P391068", "188843789408", 93, f150_2015, stored=ymm("Ford", "F-150", 2017, 2026), gate="write", write_n=10),
        Case("13", "W3611P339809", "188804158070", 94, gladiator_2018, stored=ymm("Jeep", "Gladiator", 2020, 2026), gate="write", write_n=7),
        Case("14", "W3611P334243", "188804159180", 95, ford_trucks, stored=ymm("Ford", "F-150", 2015, 2026), gate="write", write_n=12),
        Case("15", "W3611P453299", "188804220095", 107, tacoma_2016, stored=ymm("Toyota", "Tacoma", 2016, 2023), gate="write", write_n=8),
        Case("16", "W3611P511725", "188804221223", 108, cart, stored=ymm("Jeep", "Wrangler", 2008, 2026), gate="write", write_n=19),
        Case("17", "W3611P407480", "188804222503", 109, silverado_old, stored=ymm("Chevrolet", "Silverado 1500", 1999, 2013) + ymm("GMC", "Sierra 1500", 2008, 2013), gate="write", write_n=21),
        Case("18", "W3611P453304", "188804225732", 111, sienna, stored=ymm("Toyota", "Sienna", 2004, 2020), gate="write", write_n=17),
        Case("19", "W3611P453306", "188804227275", 112, highlander, description=highlander, gate="write", write_n=6),
        Case("20", "W3611P407477", "188804229010", 113, mdx_pilot, stored=ymm("Acura", "MDX", 2014, 2020) + ymm("Honda", "Pilot", 2014, 2020), gate="write", write_n=14),
        Case("21", "W3611P407484", "188804229924", 114, pilot, stored=ymm("Honda", "Pilot", 2003, 2008), gate="write", write_n=6),
        Case("N1", "W2913P449891", "188683585596", 0, "Extendable Universal Rooftop Cargo Carrier", live="not_read", counts=False, reads=0, gate="no_write"),
        Case("N2", "W3611P453305", "188752675328", 1, "Class 3 Trailer Hitch Compatible with 1983", live="not_read", counts=False, reads=0, gate="no_write"),
        Case("N3", "W465P475235", "188908662750", 2, "12V Electric Scissor Jack Kit", live="not_read", counts=False, reads=0, gate="no_write"),
        Case("N4", "W465P451130", "188770585961", 6, "Heavy Duty Snow Shovel with D-Grip", live="not_read", counts=False, reads=0, gate="no_write"),
        Case("N5", "W465P452788", "188770770096", 7, '24" Adjustable Wrench CR-V Steel', live="not_read", counts=False, reads=0, gate="no_write"),
        Case("N6", "W465P456164", "188770578826", 4, traction_jl, live="has_fitment", counts=False, reads=1, gate="no_write", live_entries=ymm("Jeep", "Wrangler", 2018, 2024)),
        Case("N7", "W465P456166", "188775682433", 19, traction_jt, live="has_fitment", counts=False, reads=1, gate="no_write", live_entries=ymm("Jeep", "Gladiator", 2020, 2026)),
        Case("N8", "W3611P407476", "188804224119", 110, rav4, live="has_fitment", counts=False, reads=1, gate="no_write", live_entries=ymm("Toyota", "RAV4", 2006, 2018)),
        Case("N9", "SYNTHETIC-READ-FAIL", "188000000099", None, "Running Boards For 2019-2026 Chevy Silverado 1500", live="read_failed", counts=False, reads=1, gate="read_failed"),
    ]


CASES = _cases()
CASES_BY_ID = {case.case_id: case for case in CASES}


def _insert(tmp_path, rows):
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE collected_products ("
        "id INTEGER PRIMARY KEY, sku TEXT, listing_id TEXT, title TEXT, "
        "description TEXT, optimization TEXT, status TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO collected_products VALUES (?,?,?,?,?,?,?)",
            (
                row["id"],
                row["sku"],
                row["listing_id"],
                row["title"],
                row["description"],
                json.dumps(row["optimization"]),
                "PUBLISHED",
            ),
        )
    conn.commit()
    conn.close()
    return db


def _position_rows():
    """Real SKUs at their DB positions, SYNTHETIC universal fillers in the gaps."""
    by_pos = {case.db_pos: case for case in CASES if case.db_pos is not None}
    rows = []
    for pos in range(0, 115):
        case = by_pos.get(pos)
        if case is not None:
            rows.append(case.row())
            continue
        rows.append(
            {
                "id": pos,
                "sku": f"SYNTHETIC-POS-{pos}",
                "listing_id": str(190000000000 + pos),
                "title": f"Heavy Duty Snow Shovel SYNTHETIC position {pos}",
                "description": "Universal shop tool.",
                "optimization": {},
            }
        )
    return rows


def _no_network(*_args, **_kwargs):
    raise AssertionError("live eBay call")


def _bind(monkeypatch, tmp_path, live, push, emails=None):
    from scripts import fitment_audit

    monkeypatch.setattr("src.utils.store_profile.get_store_profile", _aqua_auto_profile)
    monkeypatch.setattr("requests.post", _no_network)

    class FakeOAuth:
        def get_valid_token(self):
            return "token"

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda **kwargs: FakeOAuth())
    monkeypatch.setattr(fitment_audit, "get_live_compat", live)
    monkeypatch.setattr(fitment_audit, "push_compat", push)
    monkeypatch.setattr(fitment_audit, "LOG_DIR", tmp_path)
    monkeypatch.setattr(fitment_audit.time, "sleep", lambda *_a, **_k: None)
    if emails is not None:
        def _send(subject, html, send_smtp=False, **kwargs):
            emails.append({"subject": subject, "html": html, "send_smtp": send_smtp})
            return True

        monkeypatch.setattr("src.utils.email_sender.send_email", _send)
    return fitment_audit


def _scripted_live(by_item, pushed, reads, *, fail_items=None):
    from scripts import fitment_audit

    real = fitment_audit.get_live_compat
    fail_items = {str(item) for item in (fail_items or set())}

    def live(tok, item_id, *, site_id, session=None):
        item = str(item_id)
        reads.append(item)
        if item in fail_items:
            class Resp:
                status_code = 200
                text = (
                    "<GetItemResponse><Ack>Failure</Ack>"
                    "<LongMessage>Auth token is hard expired.</LongMessage>"
                    "<SeverityCode>Error</SeverityCode></GetItemResponse>"
                )

            class Sess:
                def post(self, *_a, **_k):
                    return Resp()

            return real(tok, item_id, site_id=site_id, session=Sess())
        for record in reversed(pushed):
            if record["item_id"] == item:
                return record["products"]
        return list(by_item.get(item, []))

    return live


def _recording_push(pushed):
    def push(tok, item_id, products, *, site_id, session=None):
        pushed.append(
            {
                "item_id": str(item_id),
                "products": list(products),
                "site_id": site_id,
                "token": tok,
            }
        )
        return True, "", "<xml/>"

    return push


def _report(tmp_path):
    reports = sorted(tmp_path.glob("fitment_audit_*.json"))
    assert reports, "expected a fitment audit report"
    return json.loads(reports[-1].read_text(encoding="utf-8"))


def _state(tmp_path):
    path = tmp_path / "fitment_apply_state.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_table_row_counts_reads_and_gate(case, monkeypatch, tmp_path):
    """One real-SKU (or SYNTHETIC N9) row: limit count, reads, write/gate."""
    from scripts import fitment_audit

    row_id = 1 if case.db_pos is None else case.db_pos
    db = _insert(tmp_path, [case.row(row_id)])
    pushed, reads = [], []
    by_item = {case.item_id: case.live_entries if case.live == "has_fitment" else []}
    fail = {case.item_id} if case.live == "read_failed" else set()
    _bind(
        monkeypatch,
        tmp_path,
        _scripted_live(by_item, pushed, reads, fail_items=fail),
        _recording_push(pushed),
    )
    rc = fitment_audit.main(["--apply", "--limit", "20", "--db", str(db), "--sku", case.sku])
    payload = _report(tmp_path)

    assert payload["reads_used"] == case.reads
    assert payload["candidates"] == (1 if case.counts else 0)
    if case.gate == "write":
        assert reads == [case.item_id, case.item_id]
    else:
        assert reads == ([case.item_id] * case.reads)
    if case.live == "not_read":
        assert reads == []
        assert payload["universal"] == 1
        assert payload["vehicle_specific"] == 0
        assert pushed == []
        assert rc == 0
        state = _state(tmp_path)
        assert state is None or case.item_id not in state["listings"]
        return

    state = _state(tmp_path)
    assert state and state["version"] == 1
    entry = state["listings"][case.item_id]
    assert entry["sku"] == case.sku

    if case.gate == "read_failed":
        assert rc == 1
        assert pushed == []
        assert payload["read_failed"] == 1
        assert payload["applied"] == 0
        assert payload["findings"][0]["status"] == "unknown"
        assert payload["findings"][0]["applied"] is False
        assert entry["last_result"] == "read_failed"
        return

    assert rc == 0
    if case.gate == "write":
        assert len(pushed) == 1
        assert len(pushed[0]["products"]) == case.write_n
        assert pushed[0]["item_id"] == case.item_id
        assert payload["applied"] == 1
        assert payload["findings"][0]["skip_reason"] == ""
        assert payload["findings"][0]["applied"] is True
        assert entry["last_result"] == "applied"
        return

    assert pushed == []
    assert payload["applied"] == 0
    if case.gate == "no_write":
        assert payload["has_fitment"] == 1
        assert payload["candidates"] == 0
        assert payload["findings"] == []
        assert entry["last_result"] == "has_fitment"
        return

    assert payload["findings"][0]["skip_reason"] == case.gate
    assert entry["last_result"] == case.gate
    assert case.counts is True


def test_apply_limit_20_counts_candidates_not_leading_universals(monkeypatch, tmp_path):
    """16 universal rows first, then 2 has-fitment and 22 missing. Limit 20."""
    from scripts import fitment_audit

    rows = []
    universal_ids = []
    for pos in range(16):
        item = str(191000000000 + pos)
        universal_ids.append(item)
        rows.append(
            {
                "id": pos,
                "sku": f"SYNTHETIC-UNIV-{pos}",
                "listing_id": item,
                "title": "Extendable Universal Rooftop Cargo Carrier",
                "description": "Universal carrier.",
                "optimization": {},
            }
        )
    has_ids = []
    for offset in range(2):
        pos = 16 + offset
        item = str(192000000000 + offset)
        has_ids.append(item)
        rows.append(
            {
                "id": pos,
                "sku": f"SYNTHETIC-HAS-{offset}",
                "listing_id": item,
                "title": "Off-Road Recovery Traction Boards for 2018-2024 Jeep Wrangler JL",
                "description": "Traction boards.",
                "optimization": {},
            }
        )
    missing_ids = []
    title = "Running Boards For 2019-2026 Chevy Silverado 1500"
    stored = ymm("Chevrolet", "Silverado 1500", 2019, 2026)
    for offset in range(22):
        pos = 18 + offset
        item = str(193000000000 + offset)
        missing_ids.append(item)
        rows.append(
            {
                "id": pos,
                "sku": f"SYNTHETIC-MISS-{offset}",
                "listing_id": item,
                "title": title,
                "description": "Board.",
                "optimization": _opt(stored),
            }
        )
    db = _insert(tmp_path, rows)
    pushed, reads = [], []
    by_item = {item: ymm("Jeep", "Wrangler", 2018, 2024) for item in has_ids}
    for item in missing_ids:
        by_item[item] = []
    _bind(
        monkeypatch,
        tmp_path,
        _scripted_live(by_item, pushed, reads),
        _recording_push(pushed),
    )
    rc = fitment_audit.main(["--apply", "--limit", "20", "--db", str(db)])
    payload = _report(tmp_path)
    non_missing_before = 2
    assert rc == 0
    assert payload["candidates"] == 20
    assert payload["reads_used"] == 20 + non_missing_before
    assert payload["universal"] == 16
    assert payload["has_fitment"] == non_missing_before
    assert payload["read_cap_hit"] is False
    assert not any(item in reads for item in universal_ids)
    assert [item for item in reads if item in has_ids] == has_ids
    for item in missing_ids[:20]:
        assert reads.count(item) == 2
    for item in missing_ids[20:]:
        assert item not in reads
    assert len(pushed) == 20


def test_real_order_replay_limit_20_counts_gated_skips(monkeypatch, tmp_path):
    """DB-position order: 20 candidates out of 21 missing; skips still count."""
    from scripts import fitment_audit

    db = _insert(tmp_path, _position_rows())
    pushed, reads = [], []
    by_item = {}
    for case in CASES:
        if case.db_pos is None:
            continue
        if case.live == "has_fitment":
            by_item[case.item_id] = case.live_entries
        elif case.live == "missing":
            by_item[case.item_id] = []
    _bind(
        monkeypatch,
        tmp_path,
        _scripted_live(by_item, pushed, reads),
        _recording_push(pushed),
    )
    rc = fitment_audit.main(["--apply", "--limit", "20", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 0
    assert payload["candidates"] == 20
    assert payload["reads_used"] == 23  # 20 missing + N6 + N7 + N8
    first_seen = []
    for item in reads:
        if item not in first_seen:
            first_seen.append(item)
    expected_order = [
        CASES_BY_ID[case_id].item_id
        for case_id in (
            "N6", "1", "2", "N7", "3", "4", "5", "6", "7", "8", "9", "10",
            "11", "12", "13", "14", "15", "16", "17", "N8", "18", "19", "20",
        )
    ]
    assert first_seen == expected_order
    assert CASES_BY_ID["21"].item_id not in reads
    skip_ids = {"1", "4", "8", "9", "10"}
    reasons = {row["sku"]: row.get("skip_reason") for row in payload["findings"]}
    for case_id in skip_ids:
        case = CASES_BY_ID[case_id]
        assert case.item_id in reads
        assert reasons[case.sku] == case.gate
    assert len(pushed) == 15  # 16 writers minus row 21, which sits past the cap
    filler_ids = {str(190000000000 + pos) for pos in range(115) if pos not in {c.db_pos for c in CASES}}
    assert not any(item in reads for item in filler_ids)
    for case in CASES:
        if case.live == "not_read":
            assert case.item_id not in reads


def test_rotation_never_checked_then_oldest_and_same_inputs(monkeypatch, tmp_path):
    from scripts import fitment_audit

    title = "Running Boards For 2019-2026 Chevy Silverado 1500"
    rows = [
        {
            "id": idx,
            "sku": sku,
            "listing_id": sku,
            "title": title,
            "description": "Board.",
            "optimization": {},
        }
        for idx, sku in enumerate(("A", "B", "C", "D"), start=1)
    ]

    class Clock:
        def __init__(self):
            self.n = 0

        def now(self, tz=None):
            self.n += 1
            return datetime(2026, 10, 24, 0, 0, self.n, tzinfo=timezone.utc)

    monkeypatch.setattr(fitment_audit, "datetime", Clock())
    db = _insert(tmp_path, rows)
    pushed, reads = [], []
    _bind(monkeypatch, tmp_path, _scripted_live({}, pushed, reads), _recording_push(pushed))
    assert fitment_audit.main(["--apply", "--limit", "0", "--max-reads", "2", "--db", str(db)]) == 0
    assert reads == ["A", "B"]
    first_state = _state(tmp_path)
    assert first_state["listings"]["A"]["last_checked"] < first_state["listings"]["B"]["last_checked"]

    reads.clear()
    assert fitment_audit.main(["--apply", "--limit", "0", "--max-reads", "10", "--db", str(db)]) == 0
    assert reads == ["C", "D", "A", "B"]

    # Same state file bytes always produce the same read order.
    seed = {
        "version": 1,
        "listings": {
            "B": {"sku": "B", "last_checked": "2026-10-01T00:00:00+00:00", "last_result": "has_fitment"},
            "C": {"sku": "C", "last_checked": "2026-10-03T00:00:00+00:00", "last_result": "missing"},
        },
    }
    orders = []
    for _ in range(2):
        run_dir = tmp_path / f"same-{_}"
        run_dir.mkdir()
        (run_dir / "fitment_apply_state.json").write_text(json.dumps(seed), encoding="utf-8")
        db = _insert(run_dir, rows)
        run_reads = []
        _bind(
            monkeypatch,
            run_dir,
            _scripted_live({}, [], run_reads),
            _recording_push([]),
        )
        fitment_audit.main(["--apply", "--limit", "0", "--max-reads", "10", "--db", str(db)])
        orders.append(run_reads)
    assert orders[0] == orders[1] == ["A", "D", "B", "C"]


def test_missing_or_corrupt_state_is_never_checked(monkeypatch, tmp_path, capsys):
    from scripts import fitment_audit

    title = "Steps for 2020-2026 Jeep Gladiator JT"
    rows = [
        {
            "id": idx,
            "sku": sku,
            "listing_id": sku,
            "title": title,
            "description": "Steps.",
            "optimization": {},
        }
        for idx, sku in enumerate(("A", "B"), start=1)
    ]
    payloads = [None, "not-json", {"version": 1, "listings": []}, {"version": 9, "listings": {}}]
    for index, raw in enumerate(payloads):
        run_dir = tmp_path / f"state-{index}"
        run_dir.mkdir()
        if raw is not None:
            text = raw if isinstance(raw, str) else json.dumps(raw)
            (run_dir / "fitment_apply_state.json").write_text(text, encoding="utf-8")
        db = _insert(run_dir, rows)
        reads = []
        _bind(monkeypatch, run_dir, _scripted_live({}, [], reads), _recording_push([]))
        rc = fitment_audit.main(["--apply", "--db", str(db)])
        assert rc == 0
        assert reads == ["A", "B"]
    err = capsys.readouterr().out
    assert "WARNING" in err
    assert "never checked" in err


def test_read_cap_stops_after_prewrite_reads_and_still_verifies(monkeypatch, tmp_path, capsys):
    from scripts import fitment_audit

    title = "Running Boards For 2019-2026 Chevy Silverado 1500"
    stored = ymm("Chevrolet", "Silverado 1500", 2019, 2026)
    rows = []
    for idx in range(8):
        rows.append(
            {
                "id": idx + 1,
                "sku": f"SYNTHETIC-CAP-{idx}",
                "listing_id": str(194000000000 + idx),
                "title": title,
                "description": "Board.",
                "optimization": _opt(stored),
            }
        )
    db = _insert(tmp_path, rows)
    pushed, reads = [], []
    emails = []
    _bind(
        monkeypatch,
        tmp_path,
        _scripted_live({}, pushed, reads),
        _recording_push(pushed),
        emails=emails,
    )
    rc = fitment_audit.main(
        ["--apply", "--limit", "20", "--max-reads", "5", "--email", "--db", str(db)]
    )
    payload = _report(tmp_path)
    assert rc == 0
    assert payload["read_failed"] == 0
    assert payload["reads_used"] == 5
    assert payload["candidates"] == 5
    assert payload["read_cap_hit"] is True
    assert payload["max_reads"] == 5
    assert len(pushed) == 5
    # 5 pre-write reads + 5 verify reads. Verify is not in reads_used.
    assert len(reads) == 10
    assert set(reads) == {row["listing_id"] for row in rows[:5]}
    assert "read cap 5 reached after 5 candidates" in capsys.readouterr().out
    html = emails[0]["html"]
    assert "本次读取 5/5，缺适配候选 5/20" in html
    assert "read_cap_hit: true" in html


def test_limit_zero_has_no_candidate_cap_but_reads_stay_capped(monkeypatch, tmp_path):
    from scripts import fitment_audit

    title = "Boards for 2016-2023 Toyota Tacoma"
    stored = ymm("Toyota", "Tacoma", 2016, 2023)

    def _rows(directory, count):
        rows = [
            {
                "id": idx + 1,
                "sku": f"SYNTHETIC-L0-{idx}",
                "listing_id": str(195000000000 + idx),
                "title": title,
                "description": "Board.",
                "optimization": _opt(stored),
            }
            for idx in range(count)
        ]
        return _insert(directory, rows), rows

    capped = tmp_path / "capped"
    capped.mkdir()
    db, rows = _rows(capped, 4)
    pushed, reads = [], []
    _bind(monkeypatch, capped, _scripted_live({}, pushed, reads), _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--limit", "0", "--max-reads", "2", "--db", str(db)])
    payload = _report(capped)
    assert rc == 0
    assert payload["limit"] == 0
    assert payload["candidates"] == 2
    assert payload["reads_used"] == 2
    assert payload["read_cap_hit"] is True
    assert rows[2]["listing_id"] not in reads

    open_cap = tmp_path / "open"
    open_cap.mkdir()
    db, rows = _rows(open_cap, 3)
    pushed, reads = [], []
    _bind(monkeypatch, open_cap, _scripted_live({}, pushed, reads), _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--limit", "0", "--max-reads", "60", "--db", str(db)])
    payload = _report(open_cap)
    assert rc == 0
    assert payload["candidates"] == 3
    assert payload["reads_used"] == 3
    assert payload["read_cap_hit"] is False
    assert payload["max_reads"] == 60


def test_audit_mode_keeps_raw_limit_and_report_keys(monkeypatch, tmp_path):
    from scripts import fitment_audit

    db = _insert(tmp_path, _position_rows())
    state_path = tmp_path / "fitment_apply_state.json"
    original = '{"version": 1, "listings": {"keep": {"sku": "keep", "last_checked": "2020-01-01T00:00:00+00:00", "last_result": "has_fitment"}}}\n'
    state_path.write_text(original, encoding="utf-8")
    pushed, reads = [], []
    by_item = {}
    for case in CASES:
        if case.live == "has_fitment":
            by_item[case.item_id] = case.live_entries
        elif case.live == "missing":
            by_item[case.item_id] = []
    emails = []
    _bind(
        monkeypatch,
        tmp_path,
        _scripted_live(by_item, pushed, reads),
        _recording_push(pushed),
        emails=emails,
    )
    rc = fitment_audit.main(
        ["--limit", "20", "--max-reads", "1", "--email", "--db", str(db)]
    )
    payload = _report(tmp_path)
    first_20_specific = [
        CASES_BY_ID[case_id].item_id for case_id in ("N6", "1", "2", "N7")
    ]
    assert rc == 0
    assert payload["mode"] == "audit"
    assert set(payload) == AUDIT_REPORT_KEYS
    assert reads == first_20_specific
    assert pushed == []
    assert state_path.read_text(encoding="utf-8") == original
    assert "本次读取" not in emails[0]["html"]
    assert "缺适配候选" not in emails[0]["html"]
    assert CASES_BY_ID["3"].item_id not in reads


def test_mismatch_only_is_not_written(monkeypatch, tmp_path):
    from scripts import fitment_audit

    title = "Nerf Bars for 2018-2025 Jeep Wrangler JL"
    item = "188000000701"
    db = _insert(
        tmp_path,
        [
            {
                "id": 1,
                "sku": "SYNTHETIC-MISMATCH",
                "listing_id": item,
                "title": title,
                "description": "Nerf bars.",
                "optimization": {},
            }
        ],
    )
    pushed, reads = [], []
    by_item = {item: ymm("Ford", "F-150", 2018, 2018)}
    _bind(monkeypatch, tmp_path, _scripted_live(by_item, pushed, reads), _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 0
    assert pushed == []
    assert payload["candidates"] == 0
    assert payload["has_fitment"] == 1
    assert payload["mismatch"] == 1
    assert payload["applied"] == 0
    assert _state(tmp_path)["listings"][item]["last_result"] == "has_fitment"


def test_stored_without_year_is_skipped(monkeypatch, tmp_path):
    from scripts import fitment_audit

    stored = [
        {
            "compatibilityProperties": [
                {"name": "Make", "value": "Jeep"},
                {"name": "Model", "value": "Wrangler"},
            ]
        }
    ]
    db = _insert(
        tmp_path,
        [
            {
                "id": 1,
                "sku": "SYNTHETIC-NO-YEAR-ENTRY",
                "listing_id": "188000000702",
                "title": "Nerf Bars for 2018-2025 Jeep Wrangler JL",
                "description": "Nerf bars.",
                "optimization": _opt(stored),
            }
        ],
    )
    pushed = []
    _bind(monkeypatch, tmp_path, _scripted_live({}, pushed, []), _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 0
    assert pushed == []
    assert payload["candidates"] == 1
    assert payload["findings"][0]["skip_reason"] == "skipped_no_source_fitment"
    assert payload["skipped_no_source_fitment"] == 1


def test_title_without_year_is_skipped(monkeypatch, tmp_path):
    from scripts import fitment_audit

    db = _insert(
        tmp_path,
        [
            {
                "id": 1,
                "sku": "SYNTHETIC-TITLE-NO-YEAR",
                "listing_id": "188000000703",
                "title": "Running Boards for Chevy Silverado 1500 / GMC Sierra 1500",
                "description": "Running boards.",
                "optimization": _opt(ymm("Chevrolet", "Silverado 1500", 2015, 2016)),
            }
        ],
    )
    pushed = []
    _bind(monkeypatch, tmp_path, _scripted_live({}, pushed, []), _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 0
    assert pushed == []
    assert payload["findings"][0]["skip_reason"] == "skipped_title_no_year"


def test_source_without_overlapping_ymm_is_skipped(monkeypatch, tmp_path):
    from scripts import fitment_audit

    db = _insert(
        tmp_path,
        [
            {
                "id": 1,
                "sku": "SYNTHETIC-NO-OVERLAP",
                "listing_id": "188000000704",
                "title": "Running Boards For 2019-2026 Chevy Silverado 1500",
                "description": "Fits 2005-2010 Toyota Tacoma.",
                "optimization": {},
            }
        ],
    )
    pushed = []
    _bind(monkeypatch, tmp_path, _scripted_live({}, pushed, []), _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 0
    assert pushed == []
    assert payload["findings"][0]["skip_reason"] == "skipped_no_source_fitment"


def test_apply_xml_has_no_itemspecifics(monkeypatch, tmp_path):
    from scripts import fitment_audit

    title = "Running Boards For 2019-2021 Chevy Silverado 1500"
    products = build_compatible_products(title)
    xml = fitment_audit.build_apply_xml("188000000705", products)
    assert "<ItemSpecifics>" not in xml
    assert "<Description>" not in xml
    assert "<ProductListingDetails>" not in xml
    assert "<Compatibility>" in xml

    db = _insert(
        tmp_path,
        [
            {
                "id": 1,
                "sku": "SYNTHETIC-XML",
                "listing_id": "188000000705",
                "title": title,
                "description": title,
                "optimization": {},
            }
        ],
    )
    pushed = []
    _bind(monkeypatch, tmp_path, _scripted_live({}, pushed, []), _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--db", str(db)])
    assert rc == 0
    assert len(pushed) == 1
    sent = fitment_audit.build_apply_xml(pushed[0]["item_id"], pushed[0]["products"])
    assert "<ItemSpecifics>" not in sent
    assert "<UPC>" not in sent


def test_push_ack_failure_exits_2(monkeypatch, tmp_path):
    from scripts import fitment_audit

    products = ymm("Jeep", "Wrangler", 2018, 2018)

    class Resp:
        status_code = 200
        text = "<ReviseFixedPriceItemResponse><Ack>Failure</Ack><LongMessage>Revise rejected</LongMessage></ReviseFixedPriceItemResponse>"

    class Sess:
        def post(self, *_args, **_kwargs):
            return Resp()

    ok, err, xml = fitment_audit.push_compat(
        "token", "188000000706", products, site_id="100", session=Sess()
    )
    assert ok is False
    assert "Revise rejected" in err
    assert "<ItemSpecifics>" not in xml

    db = _insert(
        tmp_path,
        [
            {
                "id": 1,
                "sku": "SYNTHETIC-ACK-FAIL",
                "listing_id": "188000000706",
                "title": "Nerf Bars for 2018-2025 Jeep Wrangler JL",
                "description": "Nerf bars.",
                "optimization": _opt(ymm("Jeep", "Wrangler", 2018, 2025)),
            }
        ],
    )

    def _fail_push(*_args, **_kwargs):
        return False, "Revise rejected", "<xml/>"

    _bind(monkeypatch, tmp_path, _scripted_live({}, [], []), _fail_push)
    rc = fitment_audit.main(["--apply", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 2
    assert payload["apply_fail"] == 1
    assert payload["applied"] == 0
    assert payload["findings"][0]["apply_error"] == "Revise rejected"
    assert _state(tmp_path)["listings"]["188000000706"]["last_result"] == "missing"


def test_readback_still_missing_is_verify_failed(monkeypatch, tmp_path):
    from scripts import fitment_audit

    title = "Boards for 2016-2023 Toyota Tacoma"
    item = "188000000707"
    db = _insert(
        tmp_path,
        [
            {
                "id": 1,
                "sku": "SYNTHETIC-VERIFY",
                "listing_id": item,
                "title": title,
                "description": "Board.",
                "optimization": _opt(ymm("Toyota", "Tacoma", 2016, 2023)),
            }
        ],
    )
    reads = []

    def live(*_args, **_kwargs):
        reads.append("read")
        return []

    pushed = []
    _bind(monkeypatch, tmp_path, live, _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 0
    assert len(pushed) == 1
    assert len(reads) == 2
    assert payload["reads_used"] == 1
    assert payload["applied"] == 1
    assert payload["verify_failed"] == 1
    assert payload["verified_ok"] == 0
    assert any(
        issue["code"] == "missing_fitment"
        for issue in payload["findings"][0]["post_apply_issues"]
    )


def test_sku_filter_uses_apply_pipeline(monkeypatch, tmp_path):
    from scripts import fitment_audit

    selected = CASES_BY_ID["3"]
    universal = CASES_BY_ID["N1"]
    other = CASES_BY_ID["5"]
    has_fit = CASES_BY_ID["N6"]
    rows = [
        universal.row(1),
        has_fit.row(2),
        selected.row(3),
        other.row(4),
    ]
    db = _insert(tmp_path, rows)
    pushed, reads = [], []
    by_item = {
        selected.item_id: [],
        other.item_id: [],
        has_fit.item_id: has_fit.live_entries,
    }
    _bind(monkeypatch, tmp_path, _scripted_live(by_item, pushed, reads), _recording_push(pushed))
    rc = fitment_audit.main(
        ["--apply", "--limit", "20", "--db", str(db), "--sku", universal.sku, "--sku", selected.sku, "--sku", "MISSING-SKU"]
    )
    payload = _report(tmp_path)
    assert rc == 0
    assert reads == [selected.item_id, selected.item_id]
    assert len(pushed) == 1
    assert len(pushed[0]["products"]) == selected.write_n
    assert payload["candidates"] == 1
    assert payload["universal"] == 1
    assert other.item_id not in reads
    assert has_fit.item_id not in reads


def test_synthetic_duplicate_listing_id_both_processed(monkeypatch, tmp_path):
    """SYNTHETIC: two PUBLISHED rows share one listing_id; id order, one state key."""
    from scripts import fitment_audit

    title = "Boards for 2016-2023 Toyota Tacoma"
    stored = ymm("Toyota", "Tacoma", 2016, 2023)
    item = "188000000801"
    rows = [
        {
            "id": 2,
            "sku": "SYNTHETIC-DUP-B",
            "listing_id": item,
            "title": title,
            "description": "Board.",
            "optimization": _opt(stored),
        },
        {
            "id": 1,
            "sku": "SYNTHETIC-DUP-A",
            "listing_id": item,
            "title": title,
            "description": "Board.",
            "optimization": _opt(stored),
        },
    ]
    db = _insert(tmp_path, rows)
    pushed, reads = [], []

    def live(_tok, item_id, *, site_id, session=None):
        reads.append(str(item_id))
        return []

    _bind(monkeypatch, tmp_path, live, _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--limit", "20", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 0
    assert reads == [item, item, item, item]
    assert payload["candidates"] == 2
    assert payload["reads_used"] == 2
    assert len(pushed) == 2
    state = _state(tmp_path)
    assert list(state["listings"]) == [item]
    assert state["listings"][item]["sku"] == "SYNTHETIC-DUP-B"


def test_synthetic_state_for_unpublished_listing_is_ignored(monkeypatch, tmp_path):
    """SYNTHETIC: state mentions a listing that is no longer PUBLISHED."""
    from scripts import fitment_audit

    title = "Steps for 2020-2026 Jeep Gladiator JT"
    rows = [
        {
            "id": 1,
            "sku": "SYNTHETIC-LIVE-A",
            "listing_id": "188000000811",
            "title": title,
            "description": "Steps.",
            "optimization": {},
        },
        {
            "id": 2,
            "sku": "SYNTHETIC-LIVE-B",
            "listing_id": "188000000812",
            "title": title,
            "description": "Steps.",
            "optimization": {},
        },
    ]
    db = _insert(tmp_path, rows)
    stale = {
        "version": 1,
        "listings": {
            "188000000899": {
                "sku": "SYNTHETIC-UNPUBLISHED",
                "last_checked": "2020-01-01T00:00:00+00:00",
                "last_result": "applied",
            }
        },
    }
    (tmp_path / "fitment_apply_state.json").write_text(json.dumps(stale), encoding="utf-8")
    reads = []
    _bind(monkeypatch, tmp_path, _scripted_live({}, [], reads), _recording_push([]))
    rc = fitment_audit.main(["--apply", "--db", str(db)])
    assert rc == 0
    assert reads == ["188000000811", "188000000812"]
    listings = _state(tmp_path)["listings"]
    assert "188000000899" in listings
    assert listings["188000000899"]["sku"] == "SYNTHETIC-UNPUBLISHED"


def test_synthetic_read_cap_consumed_by_leading_has_fitment(monkeypatch, tmp_path):
    """SYNTHETIC: --max-reads is smaller than the has-fitment rows ahead of every candidate."""
    from scripts import fitment_audit

    rows = []
    has_title = "Off-Road Traction Boards for 2020-2026 Jeep Gladiator JT"
    miss_title = "Boards for 2016-2023 Toyota Tacoma"
    for idx in range(3):
        rows.append(
            {
                "id": idx + 1,
                "sku": f"SYNTHETIC-AHEAD-{idx}",
                "listing_id": str(196000000000 + idx),
                "title": has_title,
                "description": "Boards.",
                "optimization": {},
            }
        )
    rows.append(
        {
            "id": 4,
            "sku": "SYNTHETIC-AFTER-CAP",
            "listing_id": "196000000099",
            "title": miss_title,
            "description": "Board.",
            "optimization": _opt(ymm("Toyota", "Tacoma", 2016, 2023)),
        }
    )
    db = _insert(tmp_path, rows)
    pushed, reads = [], []
    by_item = {
        row["listing_id"]: ymm("Jeep", "Gladiator", 2020, 2026) for row in rows[:3]
    }
    by_item["196000000099"] = []
    _bind(monkeypatch, tmp_path, _scripted_live(by_item, pushed, reads), _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--limit", "20", "--max-reads", "2", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 0
    assert payload["reads_used"] == 2
    assert payload["candidates"] == 0
    assert payload["has_fitment"] == 2
    assert payload["read_cap_hit"] is True
    assert pushed == []
    assert "196000000099" not in reads
    assert rows[2]["listing_id"] not in reads


def test_synthetic_state_write_failure_keeps_exit_code(monkeypatch, tmp_path, capsys):
    """SYNTHETIC: a failed atomic state write logs a warning and does not change the exit code."""
    from scripts import fitment_audit

    db = _insert(
        tmp_path,
        [
            {
                "id": 1,
                "sku": "SYNTHETIC-STATE-WRITE",
                "listing_id": "188000000821",
                "title": "Nerf Bars for 2018-2025 Jeep Wrangler JL",
                "description": "Steel nerf bar kit with mounting hardware.",
                "optimization": {},
            }
        ],
    )

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fitment_audit.os, "replace", _boom)
    pushed = []
    _bind(monkeypatch, tmp_path, _scripted_live({}, pushed, []), _recording_push(pushed))
    rc = fitment_audit.main(["--apply", "--db", str(db)])
    payload = _report(tmp_path)
    assert rc == 0
    assert pushed == []
    assert payload["findings"][0]["skip_reason"] == "skipped_no_source_fitment"
    assert "WARNING: fitment apply state write failed" in capsys.readouterr().out
    assert not (tmp_path / "fitment_apply_state.json").exists()
