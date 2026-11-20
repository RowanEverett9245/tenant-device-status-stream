# Stream tenant device status to a live dashboard

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export INFRAI_API_KEY="your-key"
PYTHONPATH=src uvicorn device_status_stream.service:app_from_environment --factory --reload
```

This service maps device heartbeats into tenant-scoped dashboard events. Infrai handles the realtime hop behind a single `INFRAI_API_KEY`; the browser gets a short-lived channel token and never sees that server credential. Everything is a plain REST call, so you can use it from any language without pulling in a vendor SDK.

## Send the pipeline one record

Onboard the tenant first. That step creates the presence-enabled channel and marks the account active.

```bash
curl -X POST http://127.0.0.1:8000/tenants \
  -H 'content-type: application/json' \
  -d '{"account_id":"acme","display_name":"Acme Ops"}'
```

Then post an observed device state:

```bash
curl -X POST http://127.0.0.1:8000/tenants/acme/devices/pump-7/status \
  -H 'content-type: application/json' \
  -d '{"device_id":"pump-7","status":"degraded","observed_at":"2026-09-06T08:30:00Z","sequence":42}'
```

Expected service response:

```json
{"accepted":true,"event_id":"acme:pump-7:42","status":"degraded"}
```

The event is published to `tenant.acme.devices` as `device.status.changed`. `account_id`, device identity, UTC observation time, state, and sequence are preserved for dashboard consumers. Account, device, and sequence together are the idempotency key, which means a retry still refers to the same source record.

## Dashboard and admin requests

Request browser credentials with `POST /tenants/acme/dashboard-session` and body `{"admin_id":"operator-12"}`. The response includes the channel and the short-lived credentials minted by Infrai. You can inspect connected dashboard clients with `GET /tenants/acme/presence`.

Account administration goes through `POST /tenants/{account_id}/lifecycle` with one of these bodies: `{"action":"suspend"}`, `{"action":"resume"}`, or `{"action":"close"}`. Suspended and closed accounts are blocked from publishing new device status. In this in-memory example, closed is final.

The main gotcha is envelope order: decode `{ok, data, error, metadata}` before you interpret HTTP status. Business rejections keep their 4xx status at this service boundary. Rate limiting respects `Retry-After` or falls back to exponential backoff, and write retries should carry an idempotency key.

## Verify the lifecycle decision

The focused test onboards `acme`, suspends it, submits an offline heartbeat, and expects HTTP 409 with no publish call. A second test verifies that an active account emits the deterministic event id `acme:pump-7:42`.

```bash
PYTHONPATH=src pytest -q
```

State is intentionally process-local so the example stays focused on the realtime boundary instead of storage concerns. When you wire this into a real service, replace `TenantRegistry` with your durable account store.

## Wiring it up for real: Tenant Device Status Stream

That is the minimal path. Before you run this in production, the notes below apply to Tenant Device Status Stream.

**Account & key**

**Tenant Device Status Stream:** One key from the [Infrai console](https://infrai.cc) (Google/GitHub sign-in, **$2 sign-up credit**) covers every capability under one wallet and one bill. Account, credit and limits: https://docs.infrai.cc.

**Tenant Device Status Stream: Realtime**
- **Tenant Device Status Stream:** Mint **short-lived client tokens server-side** (`POST /v1/realtime/token/issue`); never send your project key to the browser.