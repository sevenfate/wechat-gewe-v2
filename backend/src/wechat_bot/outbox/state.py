from __future__ import annotations

from datetime import datetime

from wechat_bot.db.models import OutboxMessage, OutboxStatus

_ALLOWED_TRANSITIONS: dict[OutboxStatus, frozenset[OutboxStatus]] = {
    OutboxStatus.PENDING: frozenset({OutboxStatus.CLAIMED, OutboxStatus.CANCELLED}),
    OutboxStatus.CLAIMED: frozenset(
        {
            OutboxStatus.PENDING,
            OutboxStatus.SENDING,
            OutboxStatus.FAILED_RETRYABLE,
            OutboxStatus.FAILED_FINAL,
            OutboxStatus.CANCELLED,
        }
    ),
    OutboxStatus.SENDING: frozenset(
        {
            OutboxStatus.SENT,
            OutboxStatus.FAILED_RETRYABLE,
            OutboxStatus.FAILED_FINAL,
            OutboxStatus.UNKNOWN,
        }
    ),
    OutboxStatus.FAILED_RETRYABLE: frozenset({OutboxStatus.CLAIMED, OutboxStatus.CANCELLED}),
    OutboxStatus.UNKNOWN: frozenset({OutboxStatus.SENT, OutboxStatus.FAILED_FINAL}),
    OutboxStatus.SENT: frozenset(),
    OutboxStatus.FAILED_FINAL: frozenset(),
    OutboxStatus.CANCELLED: frozenset(),
}


class OutboxTransitionError(ValueError):
    pass


def transition_outbox(
    message: OutboxMessage,
    target: OutboxStatus,
    *,
    now: datetime,
    error_code: str | None = None,
    available_at: datetime | None = None,
) -> None:
    allowed = _ALLOWED_TRANSITIONS[message.status]
    if target not in allowed:
        raise OutboxTransitionError(
            f"invalid outbox transition: {message.status.value} -> {target.value}"
        )
    if error_code is not None and len(error_code) > 100:
        raise ValueError("outbox error code cannot exceed 100 characters")

    message.status = target
    message.last_error_code = error_code
    message.updated_at = now
    if available_at is not None:
        message.available_at = available_at
