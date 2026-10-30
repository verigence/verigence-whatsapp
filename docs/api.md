# Verigence WhatsApp — HTTP API (`/v1`)

How other Verigence modules (Audit Core, DI, Security, …) use WhatsApp through this service.
The service owns the Meta credentials; callers never talk to Meta.

Machine-readable contract: `GET /v1/openapi.json` (needs a token).

## Authentication

```
Authorization: Bearer <token>
```

Tokens are set on the WhatsApp service with `WA_API_TOKENS`, one per calling module:

```
WA_API_TOKENS=audit-core:<secret1>,security:<secret2>
```

- Each caller gets its own token. Revoke one by removing its entry and redeploying.
- The caller name is stored on every message as `requested_by`.
- `WA_API_TOKENS` empty → every `/v1` call returns `503 WA_API_NOT_CONFIGURED`.
- A malformed value (an entry without `name:token`) stops the service at startup.
- Missing or wrong token → `401 UNAUTHENTICATED`.

## Tenancy

Every resource is under `/v1/tenants/{tenant_id}`. A caller can only see or change that tenant's data.
This is enforced twice: in the SQL, and by Postgres row-level security (`app.tenant_id`).
A resource that belongs to another tenant returns `404`, not `403`.

Note: `wa.route` and `wa.contact` have no row-level security by design (see `migrations/001_wa_schema.sql`),
so for those two tables the explicit tenant filter in the SQL is the only guard.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/tenants/{t}/messages` | Send a WhatsApp message |
| GET | `/v1/tenants/{t}/messages/{id}` | Read a sent message |
| PUT | `/v1/tenants/{t}/contacts` | Bind (or re-bind) a phone number to a Verigence user |
| GET | `/v1/tenants/{t}/contacts` | List (`status`, `user_id`, `limit`, `offset`) |
| GET / PATCH | `/v1/tenants/{t}/contacts/{id}` | Read / change `status`, `locale`, `org_unit_id` |
| DELETE | `/v1/tenants/{t}/contacts/{id}` | Revoke (sets `status=revoked`, row kept) |
| POST | `/v1/tenants/{t}/routes` | Register a WhatsApp Business number |
| GET | `/v1/tenants/{t}/routes` | List |
| PATCH | `/v1/tenants/{t}/routes/{id}` | `active`, `display_number`, `intake_journey_id` |
| GET | `/v1/tenants/{t}/sessions` | Evidence-capture sessions (`state`, `contact_id`, `deal_id`) |
| GET | `/v1/tenants/{t}/sessions/{id}` | One session |

Phone numbers are E.164 (`+919876543210`). `locale` is `en`, `hi` or `pa`.
Contact `status` is `pending`, `active`, `suspended` or `revoked`.

### Send a message

Give exactly one of `text`, `template`, `buttons`.

```bash
curl -X POST "$WA/v1/tenants/$TENANT/messages" \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: audit-4711-reminder" \
  -H "Content-Type: application/json" \
  -d '{"to":"+919876543210","text":"Your documents are complete."}'
```

```json
{"template": {"name": "doc_reminder", "language": "en", "components": []}}
{"buttons": {"body": "Confirm?", "buttons": [{"id": "yes", "title": "Yes"}]}}
```

`buttons` takes 1–3 items, each title at most 20 characters (Meta's limit).

Response `201`:

```json
{"id":"…","to":"+919876543210","kind":"text","state":"sent","wamid":"wamid.…",
 "requested_by":"audit-core","created_at":"…","sent_at":"…"}
```

The message goes out from the tenant's oldest **active** route. No active route → `409 NO_ACTIVE_ROUTE`.

**Idempotency.** Send an `Idempotency-Key` header and a retry never sends twice:

| Situation | Result |
|---|---|
| Same key, same body, already sent | `200` with the original message |
| Same key, different body | `422 IDEMPOTENCY_KEY_REUSED` |
| Same key while the first call is still running | `409 IDEMPOTENCY_KEY_IN_FLIGHT` |
| Same key after a failed send | Retried on the same record |

Keys are scoped per tenant.

**Meta failures** return `502` with `{"code","message","retryable","message_id"}`.
`retryable: true` means try again with the same key.

> **24-hour window.** Meta only allows free-form `text` / `buttons` within 24 hours of the
> recipient's last message to you. Outside it Meta rejects the send (a `502`). Use a `template` to start a conversation.
> This API does not check the window for you.

### Errors

| Status | `detail` |
|---|---|
| 401 | `UNAUTHENTICATED` |
| 404 | `MESSAGE_NOT_FOUND`, `CONTACT_NOT_FOUND`, `ROUTE_NOT_FOUND`, `SESSION_NOT_FOUND` |
| 409 | `NO_ACTIVE_ROUTE`, `PHONE_BOUND_TO_OTHER_TENANT`, `PHONE_NUMBER_ID_ALREADY_REGISTERED`, `IDEMPOTENCY_KEY_IN_FLIGHT` |
| 422 | validation errors, `IDEMPOTENCY_KEY_REUSED`, `NO_FIELDS_TO_UPDATE` |
| 502 | Meta rejected the send (object detail) |
| 503 | `WA_API_NOT_CONFIGURED` |

## Deploying

1. Set `WA_API_TOKENS` on the `wa-api` service.
2. Run migrations (`alembic upgrade head`; it is the `preDeployCommand` in `railway.toml`). Migration `003` adds
   `wa.contact.org_unit_id` and `wa.api_message`.

## Not in this API (yet)

Inbound messages are still handled only by the Meta webhook (`/wa/webhook`) and the background worker.
There is no endpoint to list inbound messages and no outbound webhook to notify other modules of them.
