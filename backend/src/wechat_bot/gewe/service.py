from __future__ import annotations

from collections.abc import Sequence

from wechat_bot.gewe.client import GeWeClient
from wechat_bot.gewe.schemas import (
    AppIdRequest,
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

    async def fetch_contacts(self, *, app_id: str) -> ContactsData:
        return await self._client.fetch_contacts(AppIdRequest(app_id=app_id))

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
