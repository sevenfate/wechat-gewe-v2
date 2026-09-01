from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence

from wechat_bot.gewe.client import GeWeClient, GeWeClientError
from wechat_bot.gewe.schemas import (
    AppIdRequest,
    BriefInfoItem,
    BriefInfoRequest,
    ChatroomInfoData,
    ChatroomInfoRequest,
    ChatroomMemberListData,
    ChatroomMemberListRequest,
    CheckLoginRequest,
    ContactsData,
    DeviceType,
    GetLoginQrCodeRequest,
    LoginQrCodeData,
    LoginStatusData,
    PostTextRequest,
    SentTextData,
)

Sleep = Callable[[float], Awaitable[None]]
BRIEF_INFO_BATCH_LIMIT = 20


class GeWeService:
    def __init__(self, client: GeWeClient) -> None:
        self._client = client

    async def get_login_qr_code(
        self,
        *,
        device_type: DeviceType,
        region_id: str,
        app_id: str = "",
        proxy_ip: str | None = None,
        ttuid: str | None = None,
        aid: str | None = None,
    ) -> LoginQrCodeData:
        return await self._client.get_login_qr_code(
            GetLoginQrCodeRequest(
                app_id=app_id,
                device_type=device_type,
                region_id=region_id,
                proxy_ip=proxy_ip,
                ttuid=ttuid,
                aid=aid,
            )
        )

    async def check_login(
        self,
        *,
        app_id: str,
        uuid: str,
        auto_sliding: bool | None = None,
        proxy_ip: str | None = None,
        captcha_code: str | None = None,
    ) -> LoginStatusData:
        return await self._client.check_login(
            CheckLoginRequest(
                app_id=app_id,
                uuid=uuid,
                auto_sliding=auto_sliding,
                proxy_ip=proxy_ip,
                captcha_code=captcha_code,
            )
        )

    async def check_online(self, *, app_id: str) -> bool:
        return await self._client.check_online(AppIdRequest(app_id=app_id))

    async def reconnect(self, *, app_id: str) -> LoginStatusData | None:
        return await self._client.reconnect(AppIdRequest(app_id=app_id))

    async def set_callback(self, *, callback_url: str) -> None:
        await self._client.set_callback(callback_url)

    async def fetch_contacts(
        self,
        *,
        app_id: str,
        cache_poll_attempts: int = 6,
        cache_poll_interval_seconds: float = 10.0,
        sleep: Sleep = asyncio.sleep,
    ) -> ContactsData:
        if cache_poll_attempts < 0:
            raise ValueError("contact cache poll attempts cannot be negative")
        if not math.isfinite(cache_poll_interval_seconds) or cache_poll_interval_seconds <= 0:
            raise ValueError("contact cache poll interval must be finite and greater than zero")

        request = AppIdRequest(app_id=app_id)
        try:
            return await self._client.fetch_contacts(request)
        except GeWeClientError as exc:
            if not exc.retryable or cache_poll_attempts == 0:
                raise
            last_error = exc

        for attempt in range(cache_poll_attempts):
            try:
                cached = await self.fetch_contacts_cache(app_id=app_id)
            except GeWeClientError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
            else:
                if cached is not None:
                    return cached

            if attempt + 1 < cache_poll_attempts:
                await sleep(cache_poll_interval_seconds)

        raise last_error

    async def fetch_contacts_cache(self, *, app_id: str) -> ContactsData | None:
        return await self._client.fetch_contacts_cache(AppIdRequest(app_id=app_id))

    async def get_brief_info(
        self,
        *,
        app_id: str,
        wxids: Sequence[str],
    ) -> list[BriefInfoItem]:
        return await self._client.get_brief_info(BriefInfoRequest(app_id=app_id, wxids=list(wxids)))

    async def get_brief_info_batched(
        self,
        *,
        app_id: str,
        wxids: Sequence[str],
        batch_size: int = BRIEF_INFO_BATCH_LIMIT,
    ) -> list[BriefInfoItem]:
        if batch_size < 1 or batch_size > BRIEF_INFO_BATCH_LIMIT:
            raise ValueError(
                f"brief info batch size must be between 1 and {BRIEF_INFO_BATCH_LIMIT}"
            )
        unique_wxids = list(dict.fromkeys(wxids))
        items: list[BriefInfoItem] = []
        for offset in range(0, len(unique_wxids), batch_size):
            items.extend(
                await self.get_brief_info(
                    app_id=app_id,
                    wxids=unique_wxids[offset : offset + batch_size],
                )
            )
        return items

    async def get_chatroom_info(
        self,
        *,
        app_id: str,
        chatroom_id: str,
    ) -> ChatroomInfoData:
        return await self._client.get_chatroom_info(
            ChatroomInfoRequest(app_id=app_id, chatroom_id=chatroom_id)
        )

    async def get_chatroom_member_list(
        self, *, app_id: str, chatroom_id: str
    ) -> ChatroomMemberListData:
        return await self._client.get_chatroom_member_list(
            ChatroomMemberListRequest(app_id=app_id, chatroom_id=chatroom_id)
        )

    async def send_text(
        self,
        *,
        app_id: str,
        to_wxid: str,
        content: str,
        at_wxids: Sequence[str] | None = None,
    ) -> SentTextData:
        ats = self._serialize_at_wxids(at_wxids)
        return await self._client.post_text(
            PostTextRequest(app_id=app_id, to_wxid=to_wxid, content=content, ats=ats)
        )

    @staticmethod
    def _serialize_at_wxids(at_wxids: Sequence[str] | None) -> str | None:
        if not at_wxids:
            return None
        return ",".join(dict.fromkeys(at_wxids))
