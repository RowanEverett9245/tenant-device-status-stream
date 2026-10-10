from typing import Any, Mapping

from fastapi.testclient import TestClient

from device_status_stream.service import create_app
from device_status_stream.infrai_realtime import InfraiError


class RecordingRealtime:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def create_channel(self, channel: str, idempotency_key: str) -> Mapping[str, Any]:
        return {"channel": channel}

    def issue_token(self, client_id: str, channel: str) -> Mapping[str, Any]:
        return {"token": "browser-session", "client_id": client_id}

    def publish_status(self, channel: str, account_id: str, data: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        self.published.append({"channel": channel, "account_id": account_id, "data": data, "idempotency_key": idempotency_key})
        return {"published": True}

    def get_presence(self, channel: str) -> Mapping[str, Any]:
        return {"members": []}


def test_suspended_tenant_cannot_publish_device_status() -> None:
    realtime = RecordingRealtime()
    client = TestClient(create_app(realtime))
    assert client.post("/tenants", json={"account_id": "acme", "display_name": "Acme Ops"}).status_code == 201
    assert client.post("/tenants/acme/lifecycle", json={"action": "suspend"}).status_code == 200

    response = client.post(
        "/tenants/acme/devices/pump-7/status",
        json={"device_id": "pump-7", "status": "offline", "observed_at": "2026-09-06T08:30:00Z", "sequence": 42},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "account is suspended"
    assert realtime.published == []


def test_active_tenant_publishes_idempotent_status_event() -> None:
    realtime = RecordingRealtime()
    client = TestClient(create_app(realtime))
    client.post("/tenants", json={"account_id": "acme", "display_name": "Acme Ops"})

    response = client.post(
        "/tenants/acme/devices/pump-7/status",
        json={"device_id": "pump-7", "status": "degraded", "observed_at": "2026-09-06T08:30:00Z", "sequence": 42},
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "event_id": "acme:pump-7:42", "status": "degraded"}
    assert realtime.published[0]["idempotency_key"] == "acme:pump-7:42"


def test_failed_channel_creation_does_not_activate_tenant() -> None:
    class FailingRealtime(RecordingRealtime):
        def create_channel(self, channel: str, idempotency_key: str) -> Mapping[str, Any]:
            raise InfraiError("INVALID_ARGUMENT", {"message": "rejected"}, 400)

    client = TestClient(create_app(FailingRealtime()))
    assert client.post("/tenants", json={"account_id": "acme", "display_name": "Acme Ops"}).status_code == 400
    assert client.post("/tenants/acme/lifecycle", json={"action": "suspend"}).status_code == 404
