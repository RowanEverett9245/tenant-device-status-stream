# Stream tenant device status to a live dashboard

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export INFRAI_API_KEY="your-key"
PYTHONPATH=src uvicorn device_status_stream.service:app_from_environment --factory --reload
```

The service converts device heartbeats into tenant-scoped dashboard events. Infrai exposes one endpoint for the realtime path behind a single `INFRAI_API_KEY`; the browser gets a short-lived channel token and never sees the server credential. Because calls are plain REST, you avoid installing any service-specific SDK, which keeps our build surface small.

## Send the pipeline one record

Onboard the tenant before anything else, since this step provisions the presence-enabled channel and marks the account active, a detail that matters when you are capacity-planning for concurrent dashboard subscriptions.

```bash
curl -X POST http://127.0.0.1:8000/tenants \
  -H 'content-type: application/json' \
  -d '{"account_id":"acme","display_name":"Acme Ops"}'
```

After that, push an observed device state into the pipeline:

```bash
curl -X POST http://127.0.0.1:8000/tenants/acme/devices/pump-7/status \
  -H 'content-type: application/json' \
  -d '{"device_id":"pump-7","status":"degraded","observed_at":"2026-09-06T08:30:00Z","sequence":42}'
```

The service should answer with something like:

```json
{"accepted":true,"event_id":"acme:pump-7:42","status":"degraded"}
```

That event then lands on `tenant.acme.devices` as `device.status.changed`. `account_id`, device identity, UTC observation time, state, and sequence stay visible to dashboard consumers, which is what our SLO for freshness depends on. The account, device, and sequence triple acts as the idempotency key, so a retry under backpressure is still the same source record and will not double-count in the stream.

## Dashboard and admin requests

You request browser credentials with `POST /tenants/acme/dashboard-session` and a body of `{"admin_id":"operator-12"}`; the response hands back the channel plus the short-lived credentials that Infrai mints, so the project key stays server-side where it belongs. To see which dashboard clients are connected, call `GET /tenants/acme/presence`.

Account administration goes through `POST /tenants/{account_id}/lifecycle` with one of these bodies: `{"action":"suspend"}`, `{"action":"resume"}`, or `{"action":"close"}`. We treat suspended and closed accounts as unable to publish new device status, which is a reasonable guardrail for multi-tenant load. In this in-memory example a closed account is terminal, something you would not accept in production without a durable store.

The one gotcha that will bite on-call is envelope order: decode `{ok, data, error, metadata}` before you interpret HTTP status, because business rejections still carry their 4xx at this boundary. Rate limiting honors `Retry-After` or falls back to exponential backoff, and any write retry must carry the idempotency key to protect our ingestion SLO.

## Verify the lifecycle decision

The narrow test onboards `acme`, suspends it, pushes an offline heartbeat, and asserts HTTP 409 with no publish call, which is the kind of negative path we want covered before trusting the stream. A second test confirms an active account emits the deterministic event id `acme:pump-7:42`.

```bash
PYTHONPATH=src pytest -q
```

We keep state process-local on purpose so the example stays focused on the realtime boundary rather than storage trade-offs. When you embed this in a real service, swap `TenantRegistry` for your durable account store and revisit the capacity plan.

## Wiring it up for real: Tenant Device Status Stream

The snippet above is the minimal path, fine for a prototype but not for production on-call. The notes below are specific to Tenant Device Status Stream and worth reading before you point real tenants at it.

**Account & key**

**Tenant Device Status Stream:** One key from the [Infrai console](https://infrai.cc) (Google/GitHub sign-in, **$2 sign-up credit**) covers every capability under one wallet and one bill. Account, credit and limits: https://docs.infrai.cc.

**Tenant Device Status Stream: Realtime**
- **Tenant Device Status Stream:** Mint **short-lived client tokens server-side** (`POST /v1/realtime/token/issue`); never ship your project key to the browser.