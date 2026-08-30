from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from wechat_bot.auth.constants import CSRF_HEADER_NAME
from wechat_bot.db.models import AuditEvent, OutboxMessage, OutboxStatus
from wechat_bot.outbox.service import OutboxService

OPERATOR_PASSWORD = "outbox operator secure password"


async def _create_account(
    client: AsyncClient,
    *,
    suffix: str,
    token: str | None = None,
) -> UUID:
    resolved_token = token or "outbox-api-secret-token"
    connection = await client.post(
        "/api/v1/connections",
        json={
            "name": f"Outbox {suffix}",
            "api_base_url": "https://api.gewe.test",
            "token": resolved_token,
        },
    )
    assert connection.status_code == 201
    account = await client.post(
        f"/api/v1/connections/{connection.json()['id']}/bot-accounts",
        json={"app_id": f"wx_app_{suffix}"},
    )
    assert account.status_code == 201
    return UUID(account.json()["id"])


async def _enqueue(
    app: FastAPI,
    account_id: UUID,
    *,
    text: str,
    state: OutboxStatus = OutboxStatus.PENDING,
) -> UUID:
    async with app.state.database.session_factory() as database, database.begin():
        message = await OutboxService().enqueue_text(
            database,
            bot_account_id=account_id,
            trace_id=uuid4(),
            idempotency_key=f"api-test:{uuid4().hex}",
            target_wxid="wxid_api_target",
            text=text,
        )
        message.status = state
        if state in {OutboxStatus.SENDING, OutboxStatus.UNKNOWN}:
            message.attempt_count = 1
        await database.flush()
        return message.id


async def _create_and_login_operator(
    client: AsyncClient,
    *,
    suffix: str,
    permission_codes: list[str],
) -> None:
    role_code = f"outbox-{suffix}"
    username = f"outbox-{suffix}-operator"
    role = await client.post(
        "/api/v1/admin/roles",
        json={"code": role_code, "name": f"Outbox {suffix}"},
    )
    assert role.status_code == 201
    permissions = await client.put(
        f"/api/v1/admin/roles/{role.json()['id']}/permissions",
        json={"permission_codes": permission_codes},
    )
    assert permissions.status_code == 200
    user = await client.post(
        "/api/v1/admin/users",
        json={
            "username": username,
            "display_name": f"Outbox {suffix} operator",
            "password": OPERATOR_PASSWORD,
        },
    )
    assert user.status_code == 201
    binding = await client.put(
        f"/api/v1/admin/users/{user.json()['id']}/roles",
        json={"role_codes": [role_code]},
    )
    assert binding.status_code == 200
    assert (await client.post("/api/auth/logout")).status_code == 200

    csrf = await client.get("/api/auth/csrf")
    login = await client.post(
        "/api/auth/login",
        headers={CSRF_HEADER_NAME: csrf.json()["csrf_token"]},
        json={"username": username, "password": OPERATOR_PASSWORD},
    )
    assert login.status_code == 200
    client.headers[CSRF_HEADER_NAME] = login.json()["csrf_token"]


async def test_outbox_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/outbox")
    assert response.status_code == 401


async def test_list_detail_pagination_and_filters(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    first_account = await _create_account(admin_client, suffix="first")
    second_account = await _create_account(admin_client, suffix="second")
    first_pending = await _enqueue(app, first_account, text="first pending")
    await _enqueue(
        app,
        first_account,
        text="first unknown",
        state=OutboxStatus.UNKNOWN,
    )
    second_pending = await _enqueue(app, second_account, text="second pending")

    first_page = await admin_client.get(
        "/api/v1/outbox",
        params={"status": "PENDING", "limit": 1, "offset": 0},
    )
    second_page = await admin_client.get(
        "/api/v1/outbox",
        params={"status": "PENDING", "limit": 1, "offset": 1},
    )
    account_filter = await admin_client.get(
        "/api/v1/outbox",
        params={"bot_account_id": str(first_account), "status": "PENDING"},
    )
    detail = await admin_client.get(f"/api/v1/outbox/{first_pending}")
    missing = await admin_client.get(f"/api/v1/outbox/{uuid4()}")

    assert first_page.status_code == 200
    assert first_page.json()["total"] == 2
    assert len(first_page.json()["items"]) == 1
    assert second_page.json()["total"] == 2
    assert {
        first_page.json()["items"][0]["id"],
        second_page.json()["items"][0]["id"],
    } == {str(first_pending), str(second_pending)}
    assert account_filter.json()["total"] == 1
    assert account_filter.json()["items"][0]["id"] == str(first_pending)
    assert detail.status_code == 200
    assert detail.json()["payload"] == {"text": "first pending", "at_wxids": []}
    assert "authorization_context" not in detail.json()
    assert missing.status_code == 404


async def test_cancel_pending_message_is_audited_without_sensitive_content(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    token = "token-that-must-not-enter-outbox-audit"
    message_text = "message-body-that-must-not-enter-outbox-audit"
    account_id = await _create_account(admin_client, suffix="cancel", token=token)
    message_id = await _enqueue(app, account_id, text=message_text)

    cancelled = await admin_client.post(
        f"/api/v1/outbox/{message_id}/cancel",
        json={"reason": "Operator stopped an obsolete response"},
    )
    repeated = await admin_client.post(
        f"/api/v1/outbox/{message_id}/cancel",
        json={"reason": "Try again"},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["last_error_code"] == "MANUAL_CANCELLED"
    assert repeated.status_code == 409

    async with app.state.database.session_factory() as database:
        events = list(
            await database.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "outbox.cancel",
                    AuditEvent.object_id == str(message_id),
                )
            )
        )
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "ADMIN_USER"
    assert event.trace_id == UUID(cancelled.json()["trace_id"])
    assert event.detail == {
        "from_status": "PENDING",
        "to_status": "CANCELLED",
        "reason": "Operator stopped an obsolete response",
    }
    serialized_audit = json.dumps(event.detail)
    assert message_text not in serialized_audit
    assert token not in serialized_audit
    assert "target_wxid" not in serialized_audit


@pytest.mark.parametrize("state", [OutboxStatus.CLAIMED, OutboxStatus.FAILED_RETRYABLE])
async def test_cancel_accepts_only_other_unsent_states(
    state: OutboxStatus,
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    account_id = await _create_account(admin_client, suffix=state.value.lower())
    message_id = await _enqueue(app, account_id, text="unsent", state=state)

    response = await admin_client.post(
        f"/api/v1/outbox/{message_id}/cancel",
        json={"reason": "No longer required"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


@pytest.mark.parametrize(
    "state",
    [
        OutboxStatus.SENDING,
        OutboxStatus.SENT,
        OutboxStatus.UNKNOWN,
        OutboxStatus.FAILED_FINAL,
        OutboxStatus.CANCELLED,
    ],
)
async def test_cancel_rejects_in_flight_or_terminal_states(
    state: OutboxStatus,
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    account_id = await _create_account(admin_client, suffix=f"blocked-{state.value.lower()}")
    message_id = await _enqueue(app, account_id, text="cannot cancel", state=state)

    response = await admin_client.post(
        f"/api/v1/outbox/{message_id}/cancel",
        json={"reason": "Attempt invalid transition"},
    )

    assert response.status_code == 409
    async with app.state.database.session_factory() as database:
        stored = await database.get(OutboxMessage, message_id)
        audits = list(
            await database.scalars(
                select(AuditEvent).where(AuditEvent.object_id == str(message_id))
            )
        )
    assert stored is not None
    assert stored.status is state
    assert audits == []


async def test_unknown_reconciliation_is_terminal_requires_reason_and_is_audited(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    account_id = await _create_account(admin_client, suffix="reconcile")
    sent_id = await _enqueue(
        app,
        account_id,
        text="uncertain sent body",
        state=OutboxStatus.UNKNOWN,
    )
    failed_id = await _enqueue(
        app,
        account_id,
        text="uncertain failed body",
        state=OutboxStatus.UNKNOWN,
    )
    pending_id = await _enqueue(app, account_id, text="still pending")
    blank_reason_id = await _enqueue(
        app,
        account_id,
        text="blank reason body",
        state=OutboxStatus.UNKNOWN,
    )

    sent = await admin_client.post(
        f"/api/v1/outbox/{sent_id}/reconcile",
        json={"resolution": "SENT", "reason": "Confirmed in the WeChat client"},
    )
    failed = await admin_client.post(
        f"/api/v1/outbox/{failed_id}/reconcile",
        json={"resolution": "FAILED_FINAL", "reason": "Confirmed absent upstream"},
    )
    repeated = await admin_client.post(
        f"/api/v1/outbox/{sent_id}/reconcile",
        json={"resolution": "FAILED_FINAL", "reason": "Conflicting second decision"},
    )
    pending = await admin_client.post(
        f"/api/v1/outbox/{pending_id}/reconcile",
        json={"resolution": "SENT", "reason": "Wrong source state"},
    )
    blank_reason = await admin_client.post(
        f"/api/v1/outbox/{blank_reason_id}/reconcile",
        json={"resolution": "SENT", "reason": "   "},
    )
    invalid_resolution = await admin_client.post(
        f"/api/v1/outbox/{blank_reason_id}/reconcile",
        json={"resolution": "PENDING", "reason": "No requeue is allowed"},
    )

    assert sent.status_code == 200
    assert sent.json()["status"] == "SENT"
    assert sent.json()["last_error_code"] == "MANUAL_RECONCILED_SENT"
    assert failed.status_code == 200
    assert failed.json()["status"] == "FAILED_FINAL"
    assert failed.json()["last_error_code"] == "MANUAL_RECONCILED_FAILED"
    assert repeated.status_code == 409
    assert pending.status_code == 409
    assert blank_reason.status_code == 422
    assert invalid_resolution.status_code == 422

    async with app.state.database.session_factory() as database:
        events = list(
            await database.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == "outbox.reconcile")
                .order_by(AuditEvent.created_at)
            )
        )
        untouched = await database.get(OutboxMessage, blank_reason_id)
    assert len(events) == 2
    assert {event.detail["to_status"] for event in events} == {
        "SENT",
        "FAILED_FINAL",
    }
    assert untouched is not None
    assert untouched.status is OutboxStatus.UNKNOWN
    audit_json = json.dumps([event.detail for event in events])
    assert "uncertain sent body" not in audit_json
    assert "uncertain failed body" not in audit_json


async def test_write_requires_csrf(app: FastAPI, admin_client: AsyncClient) -> None:
    account_id = await _create_account(admin_client, suffix="csrf")
    message_id = await _enqueue(app, account_id, text="csrf protected")
    csrf_token = admin_client.headers.pop(CSRF_HEADER_NAME)
    try:
        response = await admin_client.post(
            f"/api/v1/outbox/{message_id}/cancel",
            json={"reason": "Missing CSRF header"},
        )
    finally:
        admin_client.headers[CSRF_HEADER_NAME] = csrf_token

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed"


async def test_read_permission_does_not_grant_management(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    account_id = await _create_account(admin_client, suffix="read-only")
    message_id = await _enqueue(app, account_id, text="read only")
    await _create_and_login_operator(
        admin_client,
        suffix="reader",
        permission_codes=["outbox.read"],
    )

    assert (await admin_client.get("/api/v1/outbox")).status_code == 200
    cancel = await admin_client.post(
        f"/api/v1/outbox/{message_id}/cancel",
        json={"reason": "Reader must not cancel"},
    )
    reconcile = await admin_client.post(
        f"/api/v1/outbox/{message_id}/reconcile",
        json={"resolution": "SENT", "reason": "Reader must not reconcile"},
    )
    assert cancel.status_code == 403
    assert reconcile.status_code == 403


async def test_manage_permission_can_write_without_read_permission(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    account_id = await _create_account(admin_client, suffix="manage-only")
    message_id = await _enqueue(app, account_id, text="manage only")
    permissions = await admin_client.get("/api/v1/admin/permissions")
    permission_codes = {item["code"] for item in permissions.json()["items"]}
    assert {"outbox.read", "outbox.manage"} <= permission_codes
    await _create_and_login_operator(
        admin_client,
        suffix="manager",
        permission_codes=["outbox.manage"],
    )

    assert (await admin_client.get("/api/v1/outbox")).status_code == 403
    cancel = await admin_client.post(
        f"/api/v1/outbox/{message_id}/cancel",
        json={"reason": "Manager is allowed"},
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "CANCELLED"
