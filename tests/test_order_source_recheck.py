import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

requests_stub = types.SimpleNamespace(
    get=lambda *_args, **_kwargs: None,
    post=lambda *_args, **_kwargs: None,
    Session=object,
    utils=types.SimpleNamespace(quote=lambda value: value),
    exceptions=types.SimpleNamespace(HTTPError=Exception),
)


@pytest.fixture(autouse=True, scope="module")
def stub_sys_modules():
    stubs = {
        "dotenv": types.SimpleNamespace(load_dotenv=lambda *_args, **_kwargs: None),
    }
    saved = {}
    for name, stub in stubs.items():
        if name in sys.modules:
            saved[name] = sys.modules[name]
        sys.modules[name] = stub
    yield
    for name in stubs:
        if name in saved:
            sys.modules[name] = saved[name]
        else:
            sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def recheck():
    spec = importlib.util.spec_from_file_location(
        "order_source_recheck", ROOT / "scripts" / "order_source_recheck.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NS = "urn:ebay:apis:eBLBaseComponents"

ORDERS_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<GetOrdersResponse xmlns="{NS}">
  <Ack>Success</Ack>
  <HasMoreOrders>false</HasMoreOrders>
  <OrderArray>
    <Order>
      <OrderID>110001-000001</OrderID>
      <CreatedTime>2026-07-13T01:00:00.000Z</CreatedTime>
      <TransactionArray>
        <Transaction>
          <Item>
            <ItemID>366352780987</ItemID>
            <SKU>W3636P456662</SKU>
          </Item>
        </Transaction>
      </TransactionArray>
    </Order>
    <Order>
      <OrderID>110001-000002</OrderID>
      <CreatedTime>2026-07-13T02:00:00.000Z</CreatedTime>
      <TransactionArray>
        <Transaction>
          <Item>
            <ItemID>360000000001</ItemID>
          </Item>
          <Variation>
            <SKU>W1234P000001</SKU>
          </Variation>
        </Transaction>
      </TransactionArray>
    </Order>
  </OrderArray>
</GetOrdersResponse>"""

FAILURE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<GetOrdersResponse xmlns="{NS}">
  <Ack>Failure</Ack>
  <Errors><ShortMessage>Auth token is invalid.</ShortMessage></Errors>
</GetOrdersResponse>"""

GET_ITEM_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<GetItemResponse xmlns="{NS}">
  <Ack>Success</Ack>
  <Item>
    <ItemID>366352780987</ItemID>
    <Title>Trading Fallback Tent Title</Title>
    <Description>&lt;div&gt;Trading fallback description 177.16 x 177.16 x 110.24&lt;/div&gt;</Description>
    <ItemSpecifics>
      <NameValueList><Name>Material</Name><Value>Oxford Fabric</Value></NameValueList>
      <NameValueList><Name>Item Length</Name><Value>177.2 in</Value></NameValueList>
    </ItemSpecifics>
  </Item>
</GetItemResponse>"""


def test_parse_orders_xml_extracts_skus(recheck):
    rows, has_more = recheck.parse_orders_xml(ORDERS_XML)
    assert has_more is False
    assert rows == [
        {
            "order_id": "110001-000001",
            "sku": "W3636P456662",
            "item_id": "366352780987",
            "created": "2026-07-13T01:00:00.000Z",
        },
        {
            "order_id": "110001-000002",
            "sku": "W1234P000001",
            "item_id": "360000000001",
            "created": "2026-07-13T02:00:00.000Z",
        },
    ]


def test_parse_orders_xml_raises_on_failure_ack(recheck):
    with pytest.raises(RuntimeError, match="Auth token is invalid"):
        recheck.parse_orders_xml(FAILURE_XML)


@pytest.fixture()
def db_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "recheck.db")
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            attributes TEXT,
            specs TEXT,
            videos TEXT,
            listing_id TEXT,
            logs TEXT,
            status TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO collected_products (sku, title, description, attributes, specs, videos, listing_id, logs, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "W3636P456662",
            "600D Oxford Bell Tent with Stove Jack",
            "<div>600D Oxford cloth bell tent. PU Leather carry bag.</div>",
            json.dumps(
                {
                    "Assembled Length (in.)": "177.16",
                    "Assembled Width (in.)": "177.16",
                    "Assembled Height (in.)": "110.24",
                    "Product Weight (lbs.)": "65.48",
                    "Main Material": "oxford fabric",
                }
            ),
            "{}",
            "[]",
            "366352780987",
            "[]",
            "PUBLISHED",
        ),
    )
    conn.commit()
    yield conn
    conn.close()


class FakeEbayClient:
    def __init__(self, product=None, raise_on_inventory=False):
        self._product = product
        self._raise = raise_on_inventory

    def get_inventory_item(self, sku):
        if self._raise:
            raise RuntimeError("500 for Trading-created listing")
        if self._product is None:
            return {}
        return {"product": self._product}


def test_recheck_clean_listing_yields_no_issues(recheck, db_conn):
    ebay = FakeEbayClient(
        product={
            "title": "600D Oxford Bell Tent with Stove Jack",
            "description": "<div>600D Oxford fabric tent, 177.2 x 177.2 x 110.2 inches, 65.48 lbs.</div>",
            "aspects": {
                "Material": ["600D Oxford Fabric"],
                "Item Length": ["177.2 in"],
                "Item Width": ["177.2 in"],
                "Item Height": ["110.2 in"],
                "Item Weight": ["65.48 lbs"],
            },
        }
    )
    issues = recheck.check_sku_against_fresh_source(db_conn, ebay, "W3636P456662")
    assert issues == []


def test_recheck_detects_measurement_drift(recheck, db_conn):
    ebay = FakeEbayClient(
        product={
            "title": "600D Oxford Bell Tent with Stove Jack",
            "description": "<div>Tent</div>",
            "aspects": {"Item Length": ["157.2 in"]},
        }
    )
    issues = recheck.check_sku_against_fresh_source(db_conn, ebay, "W3636P456662")
    drift = [i for i in issues if i["type"] == "measurement_drift"]
    assert drift and drift[0]["severity"] == "CRITICAL"
    assert "157.2" in drift[0]["detail"]


def test_recheck_detects_material_hallucination(recheck, db_conn):
    ebay = FakeEbayClient(
        product={
            "title": "Genuine Leather Bell Tent with Stove Jack",
            "description": "<div>Premium genuine leather finish.</div>",
            "aspects": {"Material": ["Genuine Leather"]},
        }
    )
    issues = recheck.check_sku_against_fresh_source(db_conn, ebay, "W3636P456662")
    assert any(i["type"] == "claim_material_upgrade" and i["severity"] == "CRITICAL" for i in issues)


def test_recheck_unknown_sku_reports_missing(recheck, db_conn):
    ebay = FakeEbayClient(product=None)
    issues = recheck.check_sku_against_fresh_source(db_conn, ebay, "W0000P000000")
    assert issues and issues[0]["type"] == "sku_not_in_db"


def test_fetch_live_content_trading_fallback(recheck, db_conn, monkeypatch):
    class FakeTradingClient:
        def get_item(self, item_id):
            assert item_id == "366352780987"
            return GET_ITEM_XML

    monkeypatch.setattr(recheck, "_make_trading_client", lambda: FakeTradingClient())

    ebay = FakeEbayClient(raise_on_inventory=True)
    live = recheck._fetch_live_content(ebay, "W3636P456662", "366352780987")
    assert live is not None
    assert live["title"] == "Trading Fallback Tent Title"
    assert live["aspects"]["Material"] == ["Oxford Fabric"]


def test_ensure_and_dedupe_recheck_table(recheck, db_conn):
    recheck.ensure_recheck_table(db_conn)
    assert not recheck.already_checked(db_conn, "110001-000001", "W3636P456662")
    db_conn.execute(
        "INSERT INTO order_recheck_log (order_id, sku, issues_json, alerted) VALUES (?, ?, ?, ?)",
        ("110001-000001", "W3636P456662", "[]", 0),
    )
    db_conn.commit()
    assert recheck.already_checked(db_conn, "110001-000001", "W3636P456662")


class TestCleanRunIsVisiblyClean:
    """2026-07-27..29 出了 3 单,一封邮件都没收到。三个原因叠加:
    一次调度漏跑、一次 DNS 失败、剩下那单干净所以按设计静默。
    静默同时代表"查过没问题""从没查过""任务挂了",运营无法分辨。"""

    def test_clean_html_lists_every_checked_order(self, recheck):
        html = recheck.build_clean_html(
            [
                {"order_id": "06-14965-44302", "sku": "W2263P412380"},
                {"order_id": "17-14946-06631", "sku": "W2699P504459"},
            ]
        )
        assert "06-14965-44302" in html and "W2263P412380" in html
        assert "17-14946-06631" in html and "W2699P504459" in html
        assert "2" in html

    def test_clean_html_states_no_conflict_found(self, recheck):
        html = recheck.build_clean_html([{"order_id": "X", "sku": "Y"}])
        assert "未发现" in html

    def test_clean_html_handles_missing_fields(self, recheck):
        html = recheck.build_clean_html([{}])
        assert "<table" in html
