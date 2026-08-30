from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

import wechat_bot.directory.service as directory_service_module
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.models import (
    BotAccount,
    Chatroom,
    ChatroomMembership,
    Contact,
    GeweConnection,
)
from wechat_bot.directory.service import (
    CONTACT_TYPE_FRIEND,
    CONTACT_TYPE_OFFICIAL_ACCOUNT,
    DISCOVERED_FROM_CONTACT_LIST,
    DirectoryService,
)
from wechat_bot.gewe.schemas import (
    AppIdRequest,
    ChatroomMember,
    ChatroomMemberListData,
    ChatroomMemberListRequest,
    ContactsData,
)


class FakeSession:
    def __init__(self, *, scalar_rows: list[object] | None = None) -> None:
        self.scalar_rows = scalar_rows or []
        self.added: list[object] = []
        self.flush_count = 0

    async def scalars(self, statement: object) -> list[Any]:
        del statement
        return cast(list[Any], self.scalar_rows)

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        self.flush_count += 1


def _cipher() -> CredentialCipher:
    return CredentialCipher(Fernet.generate_key())


def _connection(cipher: CredentialCipher, token: str) -> GeweConnection:
    return GeweConnection(
        id=uuid4(),
        workspace_id=uuid4(),
        name="Test",
        api_base_url="https://api.gewe.test",
        token_ciphertext=cipher.encrypt(token),
        token_fingerprint=cipher.fingerprint(token),
        callback_secret_ciphertext=cipher.encrypt("callback-secret"),
        callback_secret_hash="callback-hash",
    )


def _account(connection: GeweConnection) -> BotAccount:
    return BotAccount(
        id=uuid4(),
        gewe_connection_id=connection.id,
        app_id="wx_app_directory",
    )


@pytest.mark.asyncio
async def test_sync_contacts_decrypts_token_and_builds_deduplicated_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "decrypted-directory-token"
    cipher = _cipher()
    connection = _connection(cipher, token)
    account = _account(connection)
    captured_tokens: list[str] = []

    class FakeGeWeClient:
        def __init__(self, *, base_url: str, token: str) -> None:
            assert base_url == connection.api_base_url
            captured_tokens.append(token)

        async def __aenter__(self) -> FakeGeWeClient:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object | None,
        ) -> None:
            del exc_type, exc, traceback

        async def fetch_contacts(self, request: AppIdRequest) -> ContactsData:
            assert request.app_id == account.app_id
            return ContactsData.model_validate(
                {
                    "friends": ["wxid_friend", "wxid_friend"],
                    "chatrooms": ["100@chatroom", "100@chatroom"],
                    "ghs": ["gh_official"],
                }
            )

    class Harness(DirectoryService):
        contact_types: dict[str, str] | None = None
        chatroom_ids: list[str] | None = None

        async def _account_connection(
            self,
            session: AsyncSession,
            bot_account_id: UUID,
        ) -> tuple[BotAccount, GeweConnection]:
            del session
            assert bot_account_id == account.id
            return account, connection

        async def _upsert_contacts(
            self,
            session: AsyncSession,
            *,
            bot_account_id: UUID,
            contact_types: dict[str, str],
            synced_at: datetime,
        ) -> None:
            del session, synced_at
            assert bot_account_id == account.id
            self.contact_types = contact_types

        async def _upsert_chatroom_placeholders(
            self,
            session: AsyncSession,
            *,
            bot_account_id: UUID,
            chatroom_ids: list[str],
            synced_at: datetime,
        ) -> None:
            del session, synced_at
            assert bot_account_id == account.id
            self.chatroom_ids = chatroom_ids

    monkeypatch.setattr(directory_service_module, "GeWeClient", FakeGeWeClient)
    fake_session = FakeSession()
    service = Harness(cipher=cipher)

    result = await service.sync_contacts(
        cast(AsyncSession, fake_session),
        account.id,
    )

    assert captured_tokens == [token]
    assert service.contact_types == {
        "wxid_friend": CONTACT_TYPE_FRIEND,
        "gh_official": CONTACT_TYPE_OFFICIAL_ACCOUNT,
    }
    assert service.chatroom_ids == ["100@chatroom"]
    assert result.observed_contacts == 2
    assert result.observed_chatrooms == 1
    assert fake_session.flush_count == 1


@pytest.mark.asyncio
async def test_sync_members_updates_group_without_claiming_complete_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "decrypted-directory-token"
    cipher = _cipher()
    connection = _connection(cipher, token)
    account = _account(connection)
    chatroom = Chatroom(
        id=uuid4(),
        bot_account_id=account.id,
        chatroom_id="100@chatroom",
        discovered_from=DISCOVERED_FROM_CONTACT_LIST,
        placeholder=True,
    )

    class FakeGeWeClient:
        def __init__(self, *, base_url: str, token: str) -> None:
            assert base_url == connection.api_base_url
            assert token == "decrypted-directory-token"

        async def __aenter__(self) -> FakeGeWeClient:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object | None,
        ) -> None:
            del exc_type, exc, traceback

        async def get_chatroom_member_list(
            self,
            request: ChatroomMemberListRequest,
        ) -> ChatroomMemberListData:
            assert request.app_id == account.app_id
            assert request.chatroom_id == chatroom.chatroom_id
            return ChatroomMemberListData.model_validate(
                {
                    "memberList": [
                        {
                            "wxid": 9007199254740993,
                            "nickName": "Member",
                            "inviterUserName": None,
                            "memberFlag": 1,
                            "displayName": None,
                            "bigHeadImgUrl": "https://example.test/big.jpg",
                            "smallHeadImgUrl": "https://example.test/small.jpg",
                        }
                    ],
                    "chatroomOwner": 9007199254740993,
                    "adminWxid": None,
                }
            )

    class Harness(DirectoryService):
        seen_member_ids: set[str] | None = None

        async def _chatroom_account_connection(
            self,
            session: AsyncSession,
            chatroom_uuid: UUID,
        ) -> tuple[Chatroom, BotAccount, GeweConnection]:
            del session
            assert chatroom_uuid == chatroom.id
            return chatroom, account, connection

        async def _upsert_memberships(
            self,
            session: AsyncSession,
            *,
            chatroom: Chatroom,
            members_by_wxid: dict[str, ChatroomMember],
        ) -> int:
            del session
            assert chatroom.id == chatroom_uuid
            self.seen_member_ids = set(members_by_wxid)
            return 3

    chatroom_uuid = chatroom.id
    monkeypatch.setattr(directory_service_module, "GeWeClient", FakeGeWeClient)
    fake_session = FakeSession()
    service = Harness(cipher=cipher)

    result = await service.sync_chatroom_members(
        cast(AsyncSession, fake_session),
        chatroom.id,
    )

    assert service.seen_member_ids == {"9007199254740993"}
    assert chatroom.owner_wxid == "9007199254740993"
    assert chatroom.member_count == 1
    assert chatroom.placeholder is True
    assert result.retained_unseen_active_members == 3
    assert result.snapshot_complete is False
    assert fake_session.flush_count == 1


@pytest.mark.asyncio
async def test_contact_and_chatroom_upserts_update_existing_and_add_new() -> None:
    cipher = _cipher()
    service = DirectoryService(cipher=cipher)
    account_id = uuid4()
    synced_at = datetime.now(UTC)
    existing_contact = Contact(
        id=uuid4(),
        bot_account_id=account_id,
        external_id="wxid_existing",
        contact_type=CONTACT_TYPE_FRIEND,
        active=False,
    )
    contact_session = FakeSession(scalar_rows=[existing_contact])

    await service._upsert_contacts(
        cast(AsyncSession, contact_session),
        bot_account_id=account_id,
        contact_types={
            "wxid_existing": CONTACT_TYPE_OFFICIAL_ACCOUNT,
            "wxid_new": CONTACT_TYPE_FRIEND,
        },
        synced_at=synced_at,
    )

    assert existing_contact.contact_type == CONTACT_TYPE_OFFICIAL_ACCOUNT
    assert existing_contact.active is True
    assert existing_contact.last_synced_at == synced_at
    new_contact = cast(Contact, contact_session.added[0])
    assert new_contact.external_id == "wxid_new"
    assert new_contact.last_synced_at == synced_at

    existing_chatroom = Chatroom(
        id=uuid4(),
        bot_account_id=account_id,
        chatroom_id="existing@chatroom",
        discovered_from="WEBHOOK",
        placeholder=False,
    )
    chatroom_session = FakeSession(scalar_rows=[existing_chatroom])
    await service._upsert_chatroom_placeholders(
        cast(AsyncSession, chatroom_session),
        bot_account_id=account_id,
        chatroom_ids=["existing@chatroom", "new@chatroom"],
        synced_at=synced_at,
    )

    assert existing_chatroom.discovered_from == "WEBHOOK"
    assert existing_chatroom.placeholder is False
    assert existing_chatroom.last_synced_at == synced_at
    new_chatroom = cast(Chatroom, chatroom_session.added[0])
    assert new_chatroom.chatroom_id == "new@chatroom"
    assert new_chatroom.discovered_from == DISCOVERED_FROM_CONTACT_LIST
    assert new_chatroom.placeholder is True


@pytest.mark.asyncio
async def test_membership_upsert_reuses_active_epoch_and_reopens_with_next_epoch() -> None:
    service = DirectoryService(cipher=_cipher())
    chatroom = Chatroom(
        id=uuid4(),
        bot_account_id=uuid4(),
        chatroom_id="100@chatroom",
        discovered_from=DISCOVERED_FROM_CONTACT_LIST,
        placeholder=True,
    )
    active_returned = ChatroomMembership(
        id=uuid4(),
        chatroom_id=chatroom.id,
        member_wxid="wxid_active",
        membership_epoch=1,
        nickname="Old active",
        left_at=None,
    )
    active_unseen = ChatroomMembership(
        id=uuid4(),
        chatroom_id=chatroom.id,
        member_wxid="wxid_unseen",
        membership_epoch=1,
        left_at=None,
    )
    previously_left = ChatroomMembership(
        id=uuid4(),
        chatroom_id=chatroom.id,
        member_wxid="wxid_rejoined",
        membership_epoch=2,
        left_at=datetime.now(UTC),
    )
    session = FakeSession(scalar_rows=[previously_left, active_returned, active_unseen])

    members = {
        "wxid_active": ChatroomMember.model_validate(
            {
                "wxid": "wxid_active",
                "nickName": "Active renamed",
                "inviterUserName": None,
                "memberFlag": 1,
                "displayName": "Active",
                "bigHeadImgUrl": "https://example.test/a-big.jpg",
                "smallHeadImgUrl": "https://example.test/a-small.jpg",
            }
        ),
        "wxid_rejoined": ChatroomMember.model_validate(
            {
                "wxid": "wxid_rejoined",
                "nickName": "Rejoined",
                "inviterUserName": "wxid_active",
                "memberFlag": 2049,
                "displayName": None,
                "bigHeadImgUrl": "https://example.test/r-big.jpg",
                "smallHeadImgUrl": "https://example.test/r-small.jpg",
            }
        ),
    }

    retained_unseen = await service._upsert_memberships(
        cast(AsyncSession, session),
        chatroom=chatroom,
        members_by_wxid=members,
    )

    assert retained_unseen == 1
    assert active_unseen.left_at is None
    assert active_returned.membership_epoch == 1
    assert active_returned.nickname == "Active renamed"
    assert len(session.added) == 1
    reopened = cast(ChatroomMembership, session.added[0])
    assert reopened.member_wxid == "wxid_rejoined"
    assert reopened.membership_epoch == 3
