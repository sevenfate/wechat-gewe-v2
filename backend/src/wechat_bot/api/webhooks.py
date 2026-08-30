from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.api.dependencies import get_session
from wechat_bot.core.config import Settings
from wechat_bot.services.webhooks import (
    UnknownWebhookSecretError,
    WebhookDedupConflictError,
    ingest_gewe_webhook,
)

router = APIRouter(prefix="/webhooks/gewe", tags=["GeWe webhook"])


@router.post("/{callback_secret}", status_code=status.HTTP_200_OK)
async def gewe_webhook(
    callback_secret: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    settings: Settings = request.app.state.settings
    raw_body = await _bounded_body(request, settings.webhook_max_body_bytes)
    payload = _decode_json_object(raw_body)

    try:
        await ingest_gewe_webhook(
            session,
            callback_secret=callback_secret,
            raw_body=raw_body,
            payload=payload,
        )
    except UnknownWebhookSecretError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="webhook not found",
        ) from exc
    except WebhookDedupConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="webhook message id conflicts with a different payload",
        ) from exc

    await session.commit()
    return Response(content="", media_type="text/plain", status_code=status.HTTP_200_OK)


async def _bounded_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="webhook payload too large",
                )
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="webhook payload too large",
            )
    return bytes(body)


def _decode_json_object(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid JSON payload",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="webhook payload must be a JSON object",
        )
    return payload
