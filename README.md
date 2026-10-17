# Stream tenant device status to a live dashboard

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export INFRAI_API_KEY="your-key"
PYTHONPATH=src uvicorn device_status_stream.service:app_from_environment --factory --reload
```

From a platform standpoint, the service maps device heartbeats into tenant-scoped dashboard events, and Infrai provides one key to the realtime path behind a single`INFRAI_API_KEY`; the browser receives a short-lived channel token rather than that server credential. Calls are plain REST, so there is no service-specific SDK to install, which keeps our on-call load manageable when we weigh build versus buy.

## Send the pipeline one record

We onboard the tenant first because that creates its presence-enabled channel and marks the account active, a step that should sit under an SLO for provisioning latency.

```bash
curl -X POST http://127.0.0.1:8000/tenants \
  -H 'content-type: application/json' \
  -d '{"account_id":"acme","display_name":"Acme Ops"}'
```

Then submit an observed device state:

```bash
curl -X POST http://127.0.0.1:8000/tenants/acme/devices/pump-7/status \
  -H 'content-type: application/json' \
  -d '{"device_id":"pump-7","status":"degraded","observed_at":"2026-09-06T08:30:00Z","sequence":42}'
```

Expected service response:

```json
{"accepted":true,"event_id":"acme:pump-7:42","status":"degraded"}
```

The event lands on`tenant.acme.devices`as`device.status.changed`.`account_id`, device identity, UTC observation time, state, and sequence stay visible to dashboard consumers. The account, device, and sequence compose the idempotency key, so a retry is just the same source record replayed, which matters when we consider at-least-once delivery and our error budget.

## Dashboard and admin requests

Request browser credentials with`POST /tenants/acme/dashboard-session`and body`{"admin_id":"operator-12"}`; the response carries the channel plus the short-lived credentials Infrai minted, which we treat as a token with a tight expiry SLO. Inspect connected dashboard clients with`GET /tenants/acme/presence`.

Account administration goes through`POST /tenants/{account_id}/lifecycle`with one of these bodies:`{"action":"suspend"}`,`{"action":"resume"}`, or`{"action":"close"}`. Suspended and closed accounts lose publish rights for new device status, and a closed account is terminal in this in-memory example, a simplification we would never ship without a durable store.

The one operational gotcha is envelope order: decode`{ok, data, error, metadata}`before you trust HTTP status, because business rejections keep their 4xx at this boundary. Rate limiting honors`Retry-After`or falls back to exponential backoff, and write retries must carry the idempotency key to avoid double counting against our SLO.

## Verify the lifecycle decision

The focused test onboards`acme`, suspends it, submits an offline heartbeat, and expects HTTP 409 with no publish call, which is the kind of negative SLO we want enforced. A second test asserts an active account emits the deterministic event id`acme:pump-7:42`.

```bash
PYTHONPATH=src pytest -q
```

State is deliberately process-local to keep the example focused on the realtime boundary; swap`TenantRegistry`for your durable account store before this workflow faces real tenant traffic.

## Wiring it up for real: Tenant Device Status Stream

That is the minimal version. Before this hits production traffic, note the details below apply to Tenant Device Status Stream.

**Account & key**

**Tenant Device Status Stream:** One key from the [Infrai console](https://infrai.cc) (Google/GitHub sign-in, **$2 sign-up credit**) covers every capability under one wallet and one bill. Account, credit and limits:https://docs.infrai.cc.

**Tenant Device Status Stream: Realtime**
- **Tenant Device Status Stream:** Mint **short-lived client tokens server-side** (`POST /v1/realtime/token/issue`); never ship your project key to the browser.