"""Integration tests for the /v1 API against a REAL Postgres.

Set WA_TEST_ADMIN_URL to a superuser DSN of a throwaway server, e.g.
    postgresql://postgres@localhost:55440/postgres
The tests create a scratch database, apply the real alembic migrations, and
connect the app as a NON-superuser role (superusers bypass row-level security,
so only a plain role can prove tenant isolation). Skipped when unset.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from wa_service.wa.client import WaApiError

ADMIN_URL = os.environ.get("WA_TEST_ADMIN_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="WA_TEST_ADMIN_URL not set")

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "tok-test-1"
TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
H = {"Authorization": f"Bearer {TOKEN}"}


class FakeClient:
    """Stands in for WaClient; records calls, can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: WaApiError | None = None
        self.n = 0

    async def _do(self, kind: str, **kw):
        self.calls.append((kind, kw))
        if self.fail:
            raise self.fail
        self.n += 1
        return type("R", (), {"wamid": f"wamid.{self.n}"})()

    async def send_text(self, **kw):
        return await self._do("text", **kw)

    async def send_template(self, **kw):
        return await self._do("template", **kw)

    async def send_interactive_buttons(self, **kw):
        return await self._do("buttons", **kw)


@pytest.fixture(scope="module")
def db():
    admin = sa.create_engine(ADMIN_URL.replace("postgresql://", "postgresql+psycopg://"), isolation_level="AUTOCOMMIT")
    name = f"wa_test_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(sa.text("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='wa_test_app') "
                          "THEN CREATE ROLE wa_test_app LOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$"))
        c.execute(sa.text(f"CREATE DATABASE {name} OWNER wa_test_app"))
    base = ADMIN_URL.rsplit("/", 1)[0]
    app_url = f"{base.replace('postgresql://', 'postgresql://wa_test_app@')}/{name}" if "@" not in base else None
    if app_url is None:  # admin url already has credentials: swap the user
        host = base.split("@", 1)[1]
        app_url = f"postgresql://wa_test_app@{host}/{name}"
    env = {**os.environ, "DATABASE_URL": app_url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True,
                   capture_output=True)
    yield app_url
    with admin.connect() as c:
        c.execute(sa.text(f"DROP DATABASE {name} WITH (FORCE)"))


@pytest.fixture()
def api(db, monkeypatch):
    for k, v in {"DATABASE_URL": db, "WA_APP_SECRET": "s", "WA_ACCESS_TOKEN": "a",
                 "WA_VERIFY_TOKEN": "v", "WA_API_TOKENS": f"audit-core:{TOKEN},security:tok-test-2"}.items():
        monkeypatch.setenv(k, v)
    from wa_service import config
    from wa_service import db as dbmod
    config.get_settings.cache_clear()
    dbmod.get_engine.cache_clear()
    from wa_service.api.routes import get_client
    from wa_service.main import create_app

    app = create_app()
    fake = FakeClient()
    app.dependency_overrides[get_client] = lambda: fake
    # skip lifespan (it would build a real Meta client)
    client = TestClient(app)
    client.fake = fake  # type: ignore[attr-defined]
    eng = dbmod.get_engine()
    for tenant in (TENANT_A, TENANT_B):  # RLS hides other tenants' rows: clean each in its own scope
        with eng.begin() as c:
            c.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
            c.execute(sa.text("DELETE FROM wa.api_message"))
            c.execute(sa.text("DELETE FROM wa.session"))
    with eng.begin() as c:  # route and contact have no RLS
        c.execute(sa.text("DELETE FROM wa.contact"))
        c.execute(sa.text("DELETE FROM wa.route"))
    return client


def _route(api, tenant=TENANT_A, pnid="PN1"):
    return api.post(f"/v1/tenants/{tenant}/routes", headers=H,
                    json={"phone_number_id": pnid, "display_number": "+91 99999 00000"})


# ---- auth -------------------------------------------------------------------
def test_no_token_is_401(api):
    assert api.get(f"/v1/tenants/{TENANT_A}/routes").status_code == 401


def test_wrong_token_is_401(api):
    r = api.get(f"/v1/tenants/{TENANT_A}/routes", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_second_caller_token_works(api):
    r = api.get(f"/v1/tenants/{TENANT_A}/routes", headers={"Authorization": "Bearer tok-test-2"})
    assert r.status_code == 200


def test_api_disabled_when_no_tokens_configured(api, monkeypatch):
    monkeypatch.setenv("WA_API_TOKENS", "")
    from wa_service import config
    config.get_settings.cache_clear()
    assert api.get(f"/v1/tenants/{TENANT_A}/routes", headers=H).status_code == 503


def test_malformed_tokens_setting_fails_fast(monkeypatch):
    monkeypatch.setenv("WA_API_TOKENS", "no-colon-here")
    for k in ("DATABASE_URL", "WA_APP_SECRET", "WA_ACCESS_TOKEN", "WA_VERIFY_TOKEN"):
        monkeypatch.setenv(k, "x")
    from wa_service import config
    config.get_settings.cache_clear()
    with pytest.raises(Exception, match="caller:token"):
        config.get_settings()


# ---- routes -----------------------------------------------------------------
def test_route_create_list_patch(api):
    r = _route(api)
    assert r.status_code == 201 and r.json()["active"] is True
    rid = r.json()["id"]
    assert [x["id"] for x in api.get(f"/v1/tenants/{TENANT_A}/routes", headers=H).json()] == [rid]
    p = api.patch(f"/v1/tenants/{TENANT_A}/routes/{rid}", headers=H, json={"active": False})
    assert p.status_code == 200 and p.json()["active"] is False


def test_duplicate_phone_number_id_is_409(api):
    assert _route(api).status_code == 201
    assert _route(api, tenant=TENANT_B).status_code == 409  # globally unique, even across tenants


def test_routes_are_tenant_isolated(api):
    _route(api, TENANT_A, "PN1")
    _route(api, TENANT_B, "PN2")
    a = api.get(f"/v1/tenants/{TENANT_A}/routes", headers=H).json()
    assert [x["phone_number_id"] for x in a] == ["PN1"]


def test_patch_other_tenants_route_is_404(api):
    rid = _route(api, TENANT_B, "PN2").json()["id"]
    assert api.patch(f"/v1/tenants/{TENANT_A}/routes/{rid}", headers=H, json={"active": False}).status_code == 404


# ---- contacts ---------------------------------------------------------------
def _contact(api, tenant=TENANT_A, phone="+919876543210", **kw):
    body = {"phone_e164": phone, "user_id": str(uuid.uuid4()), "org_unit_id": str(uuid.uuid4()), **kw}
    return api.put(f"/v1/tenants/{tenant}/contacts", headers=H, json=body)


def test_contact_bind_get_list_patch_revoke(api):
    r = _contact(api, locale="hi")
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["locale"] == "hi"
    cid = r.json()["id"]
    assert api.get(f"/v1/tenants/{TENANT_A}/contacts/{cid}", headers=H).json()["phone_e164"] == "+919876543210"
    assert len(api.get(f"/v1/tenants/{TENANT_A}/contacts?status=active", headers=H).json()) == 1
    assert api.patch(f"/v1/tenants/{TENANT_A}/contacts/{cid}", headers=H, json={"status": "suspended"}).json()["status"] == "suspended"
    assert api.delete(f"/v1/tenants/{TENANT_A}/contacts/{cid}", headers=H).status_code == 204
    assert api.get(f"/v1/tenants/{TENANT_A}/contacts/{cid}", headers=H).json()["status"] == "revoked"


def test_rebind_same_phone_updates_in_place(api):
    first = _contact(api).json()
    again = _contact(api, locale="pa").json()
    assert again["id"] == first["id"] and again["locale"] == "pa"


def test_phone_bound_to_other_tenant_is_409_and_untouched(api):
    mine = _contact(api, TENANT_A).json()
    assert _contact(api, TENANT_B).status_code == 409
    assert api.get(f"/v1/tenants/{TENANT_A}/contacts/{mine['id']}", headers=H).json()["user_id"] == mine["user_id"]


def test_contacts_tenant_isolated(api):
    _contact(api, TENANT_A, "+919876543210")
    _contact(api, TENANT_B, "+919876543211")
    assert len(api.get(f"/v1/tenants/{TENANT_A}/contacts", headers=H).json()) == 1


def test_invalid_phone_is_422(api):
    assert _contact(api, phone="9876543210").status_code == 422


def test_empty_patch_is_422(api):
    cid = _contact(api).json()["id"]
    assert api.patch(f"/v1/tenants/{TENANT_A}/contacts/{cid}", headers=H, json={}).status_code == 422


# ---- messages ---------------------------------------------------------------
def test_send_text(api):
    _route(api)
    r = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=H, json={"to": "+919876543210", "text": "hi"})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["state"] == "sent" and j["wamid"] == "wamid.1" and j["requested_by"] == "audit-core"
    kind, kw = api.fake.calls[0]
    assert kind == "text" and kw["phone_number_id"] == "PN1" and kw["to"] == "+919876543210"
    assert api.get(f"/v1/tenants/{TENANT_A}/messages/{j['id']}", headers=H).json()["wamid"] == "wamid.1"


def test_send_without_route_is_409(api):
    r = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=H, json={"to": "+919876543210", "text": "hi"})
    assert r.status_code == 409 and r.json()["detail"] == "NO_ACTIVE_ROUTE"
    assert api.fake.calls == []


def test_send_uses_only_own_tenants_route(api):
    _route(api, TENANT_B, "PN-B")
    r = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=H, json={"to": "+919876543210", "text": "hi"})
    assert r.status_code == 409 and api.fake.calls == []


def test_send_template_and_buttons(api):
    _route(api)
    t = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=H,
                 json={"to": "+919876543210", "template": {"name": "hello", "language": "en"}})
    b = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=H,
                 json={"to": "+919876543210", "buttons": {"body": "ok?", "buttons": [{"id": "y", "title": "Yes"}]}})
    assert t.status_code == 201 and b.status_code == 201
    assert [c[0] for c in api.fake.calls] == ["template", "buttons"]


@pytest.mark.parametrize("body", [
    {"to": "+919876543210"},
    {"to": "+919876543210", "text": "a", "template": {"name": "n"}},
    {"to": "bad", "text": "a"},
    {"to": "+919876543210", "text": ""},
    {"to": "+919876543210", "buttons": {"body": "b", "buttons": [{"id": str(i), "title": "t"} for i in range(4)]}},
    {"to": "+919876543210", "text": "a", "unknown": 1},
])
def test_invalid_message_bodies_are_422(api, body):
    _route(api)
    assert api.post(f"/v1/tenants/{TENANT_A}/messages", headers=H, json=body).status_code == 422
    assert api.fake.calls == []


def test_idempotent_replay_sends_once(api):
    _route(api)
    h = {**H, "Idempotency-Key": "k1"}
    body = {"to": "+919876543210", "text": "hi"}
    a = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=h, json=body)
    b = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=h, json=body)
    assert a.status_code == 201 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"] and len(api.fake.calls) == 1


def test_idempotency_key_reuse_with_different_body_is_422(api):
    _route(api)
    h = {**H, "Idempotency-Key": "k1"}
    api.post(f"/v1/tenants/{TENANT_A}/messages", headers=h, json={"to": "+919876543210", "text": "hi"})
    r = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=h, json={"to": "+919876543210", "text": "DIFFERENT"})
    assert r.status_code == 422 and len(api.fake.calls) == 1


def test_meta_failure_is_502_and_recorded_then_retry_reuses_record(api):
    _route(api)
    h = {**H, "Idempotency-Key": "k9"}
    body = {"to": "+919876543210", "text": "hi"}
    api.fake.fail = WaApiError(status_code=503, code="HTTP_ERROR", message="down", retryable=True)
    r = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=h, json=body)
    assert r.status_code == 502 and r.json()["detail"]["retryable"] is True
    mid = r.json()["detail"]["message_id"]
    assert api.get(f"/v1/tenants/{TENANT_A}/messages/{mid}", headers=H).json()["state"] == "failed"
    api.fake.fail = None
    ok = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=h, json=body)
    assert ok.status_code == 201 and ok.json()["id"] == mid and ok.json()["state"] == "sent"


def test_messages_tenant_isolated(api):
    _route(api)
    mid = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=H, json={"to": "+919876543210", "text": "hi"}).json()["id"]
    assert api.get(f"/v1/tenants/{TENANT_B}/messages/{mid}", headers=H).status_code == 404


def test_same_idempotency_key_in_different_tenants_is_independent(api):
    _route(api, TENANT_A, "PN1")
    _route(api, TENANT_B, "PN2")
    h = {**H, "Idempotency-Key": "same"}
    a = api.post(f"/v1/tenants/{TENANT_A}/messages", headers=h, json={"to": "+919876543210", "text": "hi"})
    b = api.post(f"/v1/tenants/{TENANT_B}/messages", headers=h, json={"to": "+919876543210", "text": "hi"})
    assert a.status_code == b.status_code == 201 and len(api.fake.calls) == 2


# ---- sessions ---------------------------------------------------------------
def test_sessions_read_and_isolation(api, db):
    cid = _contact(api).json()["id"]
    eng = sa.create_engine(db.replace("postgresql://", "postgresql+psycopg://"))
    with eng.begin() as c:
        c.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(TENANT_A)})
        sid = c.execute(sa.text("INSERT INTO wa.session (tenant_id, contact_id, state) VALUES (:t, :c, 'collecting') RETURNING id"),
                        {"t": TENANT_A, "c": cid}).scalar_one()
    got = api.get(f"/v1/tenants/{TENANT_A}/sessions", headers=H).json()
    assert [s["id"] for s in got] == [str(sid)]
    assert api.get(f"/v1/tenants/{TENANT_A}/sessions?state=complete", headers=H).json() == []
    assert api.get(f"/v1/tenants/{TENANT_A}/sessions/{sid}", headers=H).json()["state"] == "collecting"
    assert api.get(f"/v1/tenants/{TENANT_B}/sessions/{sid}", headers=H).status_code == 404
    assert api.get(f"/v1/tenants/{TENANT_B}/sessions", headers=H).json() == []


# ---- contract ---------------------------------------------------------------
def test_openapi_contract_lists_every_operation(api):
    r = api.get("/v1/openapi.json", headers=H)
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert set(paths) == {
        "/v1/tenants/{tenant_id}/messages", "/v1/tenants/{tenant_id}/messages/{message_id}",
        "/v1/tenants/{tenant_id}/contacts", "/v1/tenants/{tenant_id}/contacts/{contact_id}",
        "/v1/tenants/{tenant_id}/routes", "/v1/tenants/{tenant_id}/routes/{route_id}",
        "/v1/tenants/{tenant_id}/sessions", "/v1/tenants/{tenant_id}/sessions/{session_id}",
    }
    assert api.get("/v1/openapi.json").status_code == 401


def test_webhook_and_health_still_work(api):
    assert api.get("/health").json() == {"status": "ok"}
    assert api.get("/wa/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1").status_code == 403
