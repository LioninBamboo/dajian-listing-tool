import requests

from src.clients.dajian_client import DaJianClient


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"code": 200, "data": {"ok": True}}


def test_proxy_timeout_falls_back_to_direct_once(monkeypatch):
    client = DaJianClient("id", "secret")
    calls = []

    def fake_request(**kwargs):
        calls.append(kwargs.get("proxies"))
        if len(calls) == 1:
            raise requests.exceptions.Timeout("proxy stalled")
        return _Response()

    monkeypatch.setenv("DAJIAN_PROXY_URL", "http://127.0.0.1:10808")
    monkeypatch.setattr("src.clients.dajian_client._is_local_proxy_available", lambda *_: True)
    monkeypatch.setattr(client.session, "request", fake_request)

    result = client._request("GET", "/health", retries=0)

    assert result == {"ok": True}
    assert calls == [
        {"http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"},
        None,
    ]
