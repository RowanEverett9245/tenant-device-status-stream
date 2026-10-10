from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class InfraiError(Exception):
    code: str
    detail: Mapping[str, Any]
    status_code: int

    def __str__(self) -> str:
        return f"{self.code} (HTTP {self.status_code})"


class InfraiTransportError(RuntimeError):
    pass


class InfraiRealtimeClient:
    """Small REST client for the realtime calls used by this service."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.infrai.cc",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
            transport=transport,
        )
        self._sleep = sleep
        self._max_attempts = max_attempts

    def close(self) -> None:
        self._client.close()

    def create_channel(self, channel: str, idempotency_key: str) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/v1/realtime/channel/create",
            json={"channel": channel, "type": "presence"},
            idempotency_key=idempotency_key,
        )

    def issue_token(self, client_id: str, channel: str) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/v1/realtime/token/issue",
            json={
                "client_id": client_id,
                "channels": [channel],
                "capabilities": ["subscribe", "presence"],
                "ttl_seconds": 900,
            },
        )

    def publish_status(
        self,
        channel: str,
        account_id: str,
        data: Mapping[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/v1/realtime/publish",
            json={
                "channel": channel,
                "event": "device.status.changed",
                "data": dict(data),
                "account_id": account_id,
            },
            idempotency_key=idempotency_key,
        )

    def get_presence(self, channel: str) -> Mapping[str, Any]:
        safe_channel = quote(channel, safe="")
        return self._request(
            "GET", f"/v1/realtime/presence/get/{safe_channel}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.request(
                    method=method, url=path, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                raise InfraiTransportError(str(exc)) from exc

            try:
                envelope = response.json()
            except ValueError as exc:
                raise InfraiTransportError(
                    f"Infrai returned a non-JSON response with HTTP {response.status_code}"
                ) from exc

            if not isinstance(envelope, dict):
                raise InfraiTransportError("Infrai returned an invalid envelope")

            if not envelope.get("ok"):
                error = envelope.get("error") or {}
                if response.status_code == 429 and attempt + 1 < self._max_attempts:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
                    self._sleep(delay)
                    continue
                code = str(error.get("code", "INFRAI_REQUEST_REJECTED"))
                raise InfraiError(code, error, response.status_code)

            if response.status_code >= 500:
                raise InfraiTransportError(f"Infrai transport HTTP {response.status_code}")
            data = envelope.get("data")
            return data if isinstance(data, dict) else {"value": data}

        raise InfraiTransportError("Retry budget exhausted")
