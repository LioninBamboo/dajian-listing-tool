from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import probe_marketing_scope as probe


def test_classify_marketing_ok():
    assert probe.classify_marketing_response(200, '{"campaigns":[]}') == "ok"


def test_classify_marketing_missing_scope():
    body = '{"errors":[{"errorId":1100,"message":"Access denied","longMessage":"Insufficient permissions to fulfill the request."}]}'
    assert probe.classify_marketing_response(403, body) == "missing_scope"


def test_classify_marketing_invalid_scope_on_token_exchange():
    body = '{"error":"invalid_scope","error_description":"The requested scope is invalid"}'
    assert probe.classify_marketing_response(400, body) == "invalid_scope"


def test_import_refuses_main_store(tmp_path, monkeypatch):
    token_path = tmp_path / "ebay_token.json"
    token_path.write_text(json.dumps({"access_token": "abc", "refresh_token": "r"}), encoding="utf-8")

    class Profile:
        brand_name = "AquaVerve"

    monkeypatch.setattr(probe, "get_store_profile", lambda: Profile())
    saved = []
    monkeypatch.setattr(probe, "_save_token_file", lambda path: saved.append(path))

    with pytest.raises(SystemExit) as exc:
        probe.import_token(token_path, allow_main=False)
    assert "AquaVerve" in str(exc.value)
    assert saved == []


def test_reauth_scopes_omit_taxonomy():
    joined = " ".join(probe.REAUTH_SCOPES)
    assert "sell.marketing" in joined
    assert "sell.inventory" in joined
    assert "taxonomy" not in joined


def test_import_allows_substore(tmp_path, monkeypatch):
    token_path = tmp_path / "ebay_token.json"
    token_path.write_text(json.dumps({"access_token": "abc", "refresh_token": "r"}), encoding="utf-8")

    class Profile:
        brand_name = "GrovePop"

    monkeypatch.setattr(probe, "get_store_profile", lambda: Profile())
    saved = []
    monkeypatch.setattr(probe, "_save_token_file", lambda path: saved.append(Path(path)))

    probe.import_token(token_path, allow_main=False)
    assert saved == [token_path]
