from __future__ import annotations

import re
from typing import TypeVar

import httpx
from pydantic import ValidationError

from wechat_bot.gewe.schemas import (
    AppIdRequest,
    ChatroomMemberListData,
    ChatroomMemberListRequest,
    ChatroomMemberListResponse,
    CheckLoginRequest,
    CheckLoginResponse,
    CheckOnlineResponse,
    ContactsData,
    ContactsResponse,
    GetLoginQrCodeRequest,
    GeWeModel,
    GeWeResponse,
    GeWeResponseBase,
    LoginQrCodeData,
    LoginQrCodeResponse,
    LoginStatusData,
    OperationResponse,
    PostTextRequest,
    PostTextResponse,
    ReconnectionResponse,
    SentTextData,
    SetCallbackRequest,
)

_TOKEN_HEADER = "X-GEWE-TOKEN"  # noqa: S105  # Header name, not a credential.
_TOKEN_PATTERN = re.compile(r"(?i)(token\s*[:=]\s*)[^\s,;]+")
_ResponseT = TypeVar("_ResponseT", bound=GeWeResponseBase)
_DataT = TypeVar("_DataT")


class GeWeClientError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class GeWeTransportError(GeWeClientError):
    pass


class GeWeHTTPError(GeWeClientError):
    def __init__(self, status_code: int, *, retryable: bool) -> None:
        super().__init__(f"GeWe HTTP request failed with status {status_code}", retryable=retryable)
        self.status_code = status_code


class GeWeAPIError(GeWeClientError):
    def __init__(self, ret: int, provider_message: str, *, retryable: bool) -> None:
        super().__init__(f"GeWe API returned ret={ret}: {provider_message}", retryable=retryable)
        self.ret = ret
        self.provider_message = provider_message


class GeWeProtocolError(GeWeClientError):
    pass


def _is_retryable_status(code: int) -> bool:
    return code in {408, 425, 429} or 500 <= code <= 599


class GeWeClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 20.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("GeWe token cannot be empty")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_http_client = http_client is None

    async def __aenter__(self) -> GeWeClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def get_login_qr_code(self, request: GetLoginQrCodeRequest) -> LoginQrCodeData:
        response = await self._post(
            "/gewe/v2/api/login/getLoginQrCode", request, LoginQrCodeResponse
        )
        return self._require_data(response)

    async def check_login(self, request: CheckLoginRequest) -> LoginStatusData:
        response = await self._post("/gewe/v2/api/login/checkLogin", request, CheckLoginResponse)
        return self._require_data(response)

    async def check_online(self, request: AppIdRequest) -> bool:
        response = await self._post("/gewe/v2/api/login/checkOnline", request, CheckOnlineResponse)
        return self._require_data(response)

    async def reconnect(self, request: AppIdRequest) -> LoginStatusData | None:
        response = await self._post(
            "/gewe/v2/api/login/reconnection", request, ReconnectionResponse
        )
        return response.data

    async def set_callback(self, callback_url: str) -> None:
        request = SetCallbackRequest(token=self._token, callback_url=callback_url)
        await self._post("/gewe/v2/api/login/setCallback", request, OperationResponse)

    async def fetch_contacts(self, request: AppIdRequest) -> ContactsData:
        response = await self._post(
            "/gewe/v2/api/contacts/fetchContactsList", request, ContactsResponse
        )
        return self._require_data(response)

    async def get_chatroom_member_list(
        self, request: ChatroomMemberListRequest
    ) -> ChatroomMemberListData:
        response = await self._post(
            "/gewe/v2/api/group/getChatroomMemberList",
            request,
            ChatroomMemberListResponse,
        )
        return self._require_data(response)

    async def post_text(self, request: PostTextRequest) -> SentTextData:
        response = await self._post("/gewe/v2/api/message/postText", request, PostTextResponse)
        return self._require_data(response)

    async def _post(
        self,
        path: str,
        request: GeWeModel,
        response_model: type[_ResponseT],
    ) -> _ResponseT:
        try:
            response = await self._http_client.post(
                f"{self._base_url}{path}",
                headers={_TOKEN_HEADER: self._token},
                json=request.model_dump(by_alias=True, exclude_none=True, mode="json"),
            )
        except httpx.TimeoutException as exc:
            raise GeWeTransportError("GeWe request timed out", retryable=True) from exc
        except httpx.RequestError as exc:
            retryable = isinstance(
                exc,
                (httpx.NetworkError, httpx.RemoteProtocolError),
            )
            raise GeWeTransportError("GeWe network request failed", retryable=retryable) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise GeWeHTTPError(
                response.status_code,
                retryable=_is_retryable_status(response.status_code),
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise GeWeProtocolError("GeWe returned invalid JSON", retryable=False) from exc

        try:
            parsed = response_model.model_validate(payload)
        except ValidationError as exc:
            raise GeWeProtocolError(
                "GeWe response did not match the expected contract", retryable=False
            ) from exc

        if parsed.ret != 200:
            raise GeWeAPIError(
                parsed.ret,
                self._redact(parsed.msg),
                retryable=_is_retryable_status(parsed.ret),
            )
        return parsed

    def _require_data(self, response: GeWeResponse[_DataT]) -> _DataT:
        if response.data is None:
            raise GeWeProtocolError("GeWe response omitted required data", retryable=False)
        return response.data

    def _redact(self, message: str) -> str:
        redacted = message.replace(self._token, "[REDACTED]")
        return _TOKEN_PATTERN.sub(r"\1[REDACTED]", redacted)[:500]
