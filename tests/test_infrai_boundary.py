import httpx
import json
import pytest

from device_status_stream.infrai_realtime import InfraiError, InfraiRealtimeClient


def test_business_rejection_is_decoded_before_http_status() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.content) == {"channel": "tenant.acme.devices", "type": "presence"}
        return httpx.Response(
            400,
            json={"ok": False, "data": None, "error": {"code": "INVALID_ARGUMENT", "message": "channel is required"}, "metadata": {}},
        )

    client = InfraiRealtimeClient("test-key", transport=httpx.MockTransport(reject))
    with pytest.raises(InfraiError) as caught:
        client.create_channel("tenant.acme.devices", "tenant-onboard:acme")
    client.close()

    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.status_code == 400


def test_rate_limit_honors_retry_after_and_reuses_idempotency_key() -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"ok": False, "data": None, "error": {"code": "RATE_LIMITED"}, "metadata": {}})
        return httpx.Response(200, json={"ok": True, "data": {"published": True}, "error": None, "metadata": {}})

    client = InfraiRealtimeClient("test-key", transport=httpx.MockTransport(respond), sleep=delays.append)
    result = client.publish_status("tenant.acme.devices", "acme", {"device_id": "pump-7"}, "acme:pump-7:42")
    client.close()

    assert result == {"published": True}
    assert delays == [2.0]
    assert [request.headers["Idempotency-Key"] for request in requests] == ["acme:pump-7:42", "acme:pump-7:42"]
