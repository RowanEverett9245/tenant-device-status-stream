from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .infrai_realtime import InfraiError, InfraiRealtimeClient, InfraiTransportError


class AccountState(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class DeviceState(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class TenantOnboarding(BaseModel):
    account_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)


class AccountAction(BaseModel):
    action: str


class DeviceHeartbeat(BaseModel):
    device_id: str = Field(min_length=1, max_length=120)
    status: DeviceState
    observed_at: datetime
    sequence: int = Field(ge=0)


class DashboardSession(BaseModel):
    admin_id: str = Field(min_length=1, max_length=120)


class RealtimePort(Protocol):
    def create_channel(self, channel: str, idempotency_key: str) -> Mapping[str, Any]:
        """Create a tenant channel."""

    def issue_token(self, client_id: str, channel: str) -> Mapping[str, Any]:
        """Issue browser credentials for one channel."""

    def publish_status(self, channel: str, account_id: str, data: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        """Publish one normalized status event."""

    def get_presence(self, channel: str) -> Mapping[str, Any]:
        """Read the channel presence set."""


class TenantRegistry:
    def __init__(self) -> None:
        self._accounts: dict[str, AccountState] = {}

    def onboard(self, account_id: str) -> None:
        if account_id in self._accounts:
            raise ValueError("account already exists")
        self._accounts[account_id] = AccountState.ACTIVE

    def undo_onboard(self, account_id: str) -> None:
        del self._accounts[account_id]

    def transition(self, account_id: str, action: str) -> AccountState:
        current = self.require(account_id)
        transitions = {
            (AccountState.ACTIVE, "suspend"): AccountState.SUSPENDED,
            (AccountState.SUSPENDED, "resume"): AccountState.ACTIVE,
            (AccountState.ACTIVE, "close"): AccountState.CLOSED,
            (AccountState.SUSPENDED, "close"): AccountState.CLOSED,
        }
        next_state = transitions.get((current, action))
        if next_state is None:
            raise ValueError(f"cannot {action} an account in {current.value} state")
        self._accounts[account_id] = next_state
        return next_state

    def require(self, account_id: str) -> AccountState:
        try:
            return self._accounts[account_id]
        except KeyError as exc:
            raise KeyError("account not found") from exc


def channel_for(account_id: str) -> str:
    return f"tenant.{account_id}.devices"


def create_app(realtime: RealtimePort) -> FastAPI:
    api = FastAPI(title="Tenant device status stream")
    tenants = TenantRegistry()

    @api.post("/tenants", status_code=201)
    def onboard_tenant(request: TenantOnboarding) -> dict[str, Any]:
        try:
            tenants.onboard(request.account_id)
            channel = channel_for(request.account_id)
            try:
                realtime.create_channel(channel, f"tenant-onboard:{request.account_id}")
            except (InfraiError, InfraiTransportError):
                tenants.undo_onboard(request.account_id)
                raise
            return {"account_id": request.account_id, "state": "active", "channel": channel}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InfraiError, InfraiTransportError) as exc:
            raise upstream_http_error(exc) from exc

    @api.post("/tenants/{account_id}/lifecycle")
    def change_lifecycle(account_id: str, request: AccountAction) -> dict[str, str]:
        try:
            state = tenants.transition(account_id, request.action)
            return {"account_id": account_id, "state": state.value}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/tenants/{account_id}/devices/{device_id}/status", status_code=202)
    def ingest_status(account_id: str, device_id: str, heartbeat: DeviceHeartbeat) -> dict[str, Any]:
        try:
            state = tenants.require(account_id)
            if state is not AccountState.ACTIVE:
                raise HTTPException(status_code=409, detail=f"account is {state.value}")
            if heartbeat.device_id != device_id:
                raise HTTPException(status_code=422, detail="device_id must match the path")
            observed_at = heartbeat.observed_at.astimezone(timezone.utc).isoformat()
            event_id = f"{account_id}:{device_id}:{heartbeat.sequence}"
            realtime.publish_status(
                channel_for(account_id),
                account_id,
                {"device_id": device_id, "status": heartbeat.status.value, "observed_at": observed_at, "sequence": heartbeat.sequence},
                event_id,
            )
            return {"accepted": True, "event_id": event_id, "status": heartbeat.status.value}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InfraiError, InfraiTransportError) as exc:
            raise upstream_http_error(exc) from exc

    @api.post("/tenants/{account_id}/dashboard-session")
    def dashboard_session(account_id: str, request: DashboardSession) -> dict[str, Any]:
        try:
            if tenants.require(account_id) is not AccountState.ACTIVE:
                raise HTTPException(status_code=409, detail="account is not active")
            channel = channel_for(account_id)
            token = realtime.issue_token(f"admin:{request.admin_id}", channel)
            return {"channel": channel, "credentials": token}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InfraiError, InfraiTransportError) as exc:
            raise upstream_http_error(exc) from exc

    @api.get("/tenants/{account_id}/presence")
    def tenant_presence(account_id: str) -> dict[str, Any]:
        try:
            tenants.require(account_id)
            return {"channel": channel_for(account_id), "presence": realtime.get_presence(channel_for(account_id))}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InfraiError, InfraiTransportError) as exc:
            raise upstream_http_error(exc) from exc

    return api


def upstream_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, InfraiError):
        status = exc.status_code if 400 <= exc.status_code < 500 else 502
        return HTTPException(status_code=status, detail={"code": exc.code, "error": dict(exc.detail)})
    return HTTPException(status_code=502, detail="realtime transport error")


def app_from_environment() -> FastAPI:
    api_key = os.environ.get("INFRAI_API_KEY")
    if not api_key:
        raise RuntimeError("INFRAI_API_KEY is required")
    return create_app(InfraiRealtimeClient(api_key))
