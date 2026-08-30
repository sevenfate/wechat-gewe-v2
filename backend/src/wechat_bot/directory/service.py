from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.auth.service import AuthPrincipal
from wechat_bot.core.crypto import CredentialCipher, CredentialDecryptionError
from wechat_bot.db.base import utc_now
from wechat_bot.db.models import (
    AuditEvent,
    BotAccount,
    Chatroom,
    ChatroomMembership,
    Contact,
    GeweConnection,
)
from wechat_bot.directory.schemas import (
    ChatroomList,
    ChatroomView,
    ContactList,
    ContactView,
    DirectorySyncResult,
    MembershipList,
    MembershipSyncResult,
    MembershipView,
)
from wechat_bot.gewe.client import GeWeClient
from wechat_bot.gewe.schemas import AppIdRequest, ChatroomMember, ChatroomMemberListRequest

CONTACT_TYPE_FRIEND = "FRIEND"
CONTACT_TYPE_OFFICIAL_ACCOUNT = "OFFICIAL_ACCOUNT"
DISCOVERED_FROM_CONTACT_LIST = "CONTACT_LIST"


class DirectoryNotFoundError(LookupError):
    def __init__(self, resource: str) -> None:
        super().__init__(f"{resource} not found")
        self.resource = resource


class DirectoryCredentialError(RuntimeError):
    pass


class DirectoryMembershipConflictError(RuntimeError):
    pass


@dataclass(slots=True)
class DirectoryService:
    cipher: CredentialCipher

    async def sync_contacts(
        self,
        session: AsyncSession,
        bot_account_id: UUID,
    ) -> DirectorySyncResult:
        account, connection = await self._account_connection(session, bot_account_id)
        token = self._decrypt_token(connection)
        async with GeWeClient(base_url=connection.api_base_url, token=token) as client:
            directory = await client.fetch_contacts(AppIdRequest(app_id=account.app_id))

        synced_at = utc_now()
        contact_types = dict.fromkeys(directory.friends, CONTACT_TYPE_FRIEND)
        contact_types.update(
            dict.fromkeys(directory.official_accounts, CONTACT_TYPE_OFFICIAL_ACCOUNT)
        )
        await self._upsert_contacts(
            session,
            bot_account_id=account.id,
            contact_types=contact_types,
            synced_at=synced_at,
        )
        chatroom_ids = list(dict.fromkeys(directory.chatrooms))
        await self._upsert_chatroom_placeholders(
            session,
            bot_account_id=account.id,
            chatroom_ids=chatroom_ids,
            synced_at=synced_at,
        )
        await session.flush()
        return DirectorySyncResult(
            bot_account_id=account.id,
            observed_contacts=len(contact_types),
            observed_chatrooms=len(chatroom_ids),
            synced_at=synced_at,
        )

    async def sync_chatroom_members(
        self,
        session: AsyncSession,
        chatroom_uuid: UUID,
    ) -> MembershipSyncResult:
        chatroom, account, connection = await self._chatroom_account_connection(
            session, chatroom_uuid
        )
        token = self._decrypt_token(connection)
        async with GeWeClient(base_url=connection.api_base_url, token=token) as client:
            snapshot = await client.get_chatroom_member_list(
                ChatroomMemberListRequest(
                    app_id=account.app_id,
                    chatroom_id=chatroom.chatroom_id,
                )
            )

        synced_at = utc_now()
        members_by_wxid = {member.wxid: member for member in snapshot.members}
        retained_unseen = await self._upsert_memberships(
            session,
            chatroom=chatroom,
            members_by_wxid=members_by_wxid,
        )
        chatroom.owner_wxid = snapshot.owner_wxid
        chatroom.member_count = len(members_by_wxid)
        chatroom.last_synced_at = synced_at
        await session.flush()
        return MembershipSyncResult(
            chatroom_id=chatroom.id,
            observed_members=len(members_by_wxid),
            retained_unseen_active_members=retained_unseen,
            snapshot_complete=False,
            synced_at=synced_at,
        )

    async def list_contacts(
        self,
        session: AsyncSession,
        bot_account_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> ContactList:
        await self._require_account(session, bot_account_id)
        criteria = Contact.bot_account_id == bot_account_id
        items = list(
            await session.scalars(
                select(Contact)
                .where(criteria)
                .order_by(Contact.external_id.asc(), Contact.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(select(func.count()).select_from(Contact).where(criteria))
        return ContactList(
            items=[ContactView.model_validate(item) for item in items],
            total=total or 0,
        )

    async def list_chatrooms(
        self,
        session: AsyncSession,
        bot_account_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> ChatroomList:
        await self._require_account(session, bot_account_id)
        criteria = Chatroom.bot_account_id == bot_account_id
        items = list(
            await session.scalars(
                select(Chatroom)
                .where(criteria)
                .order_by(Chatroom.chatroom_id.asc(), Chatroom.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(select(func.count()).select_from(Chatroom).where(criteria))
        return ChatroomList(
            items=[ChatroomView.model_validate(item) for item in items],
            total=total or 0,
        )

    async def list_memberships(
        self,
        session: AsyncSession,
        chatroom_uuid: UUID,
        *,
        include_left: bool,
        limit: int,
        offset: int,
    ) -> MembershipList:
        await self._require_chatroom(session, chatroom_uuid)
        criteria = [ChatroomMembership.chatroom_id == chatroom_uuid]
        if not include_left:
            criteria.append(ChatroomMembership.left_at.is_(None))
        items = list(
            await session.scalars(
                select(ChatroomMembership)
                .where(*criteria)
                .order_by(
                    ChatroomMembership.member_wxid.asc(),
                    ChatroomMembership.membership_epoch.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(
            select(func.count()).select_from(ChatroomMembership).where(*criteria)
        )
        return MembershipList(
            items=[self._membership_view(item) for item in items],
            total=total or 0,
        )

    async def mark_membership_left(
        self,
        session: AsyncSession,
        chatroom_uuid: UUID,
        membership_uuid: UUID,
        *,
        membership_epoch: int,
        reason: str,
        actor: AuthPrincipal,
    ) -> MembershipView:
        row = (
            await session.execute(
                select(ChatroomMembership, GeweConnection.workspace_id)
                .join(Chatroom, Chatroom.id == ChatroomMembership.chatroom_id)
                .join(BotAccount, BotAccount.id == Chatroom.bot_account_id)
                .join(
                    GeweConnection,
                    GeweConnection.id == BotAccount.gewe_connection_id,
                )
                .where(
                    Chatroom.id == chatroom_uuid,
                    ChatroomMembership.id == membership_uuid,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise DirectoryNotFoundError("chatroom membership")
        membership, workspace_id = row

        active_memberships = list(
            await session.scalars(
                select(ChatroomMembership)
                .where(
                    ChatroomMembership.chatroom_id == chatroom_uuid,
                    ChatroomMembership.member_wxid == membership.member_wxid,
                    ChatroomMembership.left_at.is_(None),
                )
                .order_by(ChatroomMembership.membership_epoch.desc())
                .with_for_update()
            )
        )
        current = active_memberships[0] if active_memberships else None
        if (
            membership.left_at is not None
            or membership.membership_epoch != membership_epoch
            or current is None
            or current.id != membership.id
        ):
            raise DirectoryMembershipConflictError(
                "membership is no longer active at the expected epoch"
            )

        departed_at = utc_now()
        for active_membership in active_memberships:
            active_membership.left_at = departed_at
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                trace_id=None,
                actor_type="ADMIN_USER",
                actor_id=str(actor.user_id),
                action="directory.membership.mark_left",
                object_type="chatroom_membership",
                object_id=str(membership.id),
                result="SUCCESS",
                detail={
                    "chatroom_id": str(chatroom_uuid),
                    "member_wxid": membership.member_wxid,
                    "membership_epoch": membership.membership_epoch,
                    "operator_username": actor.username,
                    "reason": reason,
                    "closed_active_memberships": len(active_memberships),
                },
            )
        )
        await session.flush()
        return self._membership_view(membership)

    async def _upsert_contacts(
        self,
        session: AsyncSession,
        *,
        bot_account_id: UUID,
        contact_types: dict[str, str],
        synced_at: datetime,
    ) -> None:
        if not contact_types:
            return
        existing = {
            contact.external_id: contact
            for contact in await session.scalars(
                select(Contact).where(
                    Contact.bot_account_id == bot_account_id,
                    Contact.external_id.in_(contact_types),
                )
            )
        }
        for external_id, contact_type in contact_types.items():
            contact = existing.get(external_id)
            if contact is None:
                contact = Contact(
                    bot_account_id=bot_account_id,
                    external_id=external_id,
                    contact_type=contact_type,
                )
                session.add(contact)
            else:
                contact.contact_type = contact_type
            contact.active = True
            contact.last_synced_at = synced_at

    async def _upsert_chatroom_placeholders(
        self,
        session: AsyncSession,
        *,
        bot_account_id: UUID,
        chatroom_ids: list[str],
        synced_at: datetime,
    ) -> None:
        if not chatroom_ids:
            return
        existing = {
            chatroom.chatroom_id: chatroom
            for chatroom in await session.scalars(
                select(Chatroom).where(
                    Chatroom.bot_account_id == bot_account_id,
                    Chatroom.chatroom_id.in_(chatroom_ids),
                )
            )
        }
        for chatroom_id in chatroom_ids:
            chatroom = existing.get(chatroom_id)
            if chatroom is None:
                chatroom = Chatroom(
                    bot_account_id=bot_account_id,
                    chatroom_id=chatroom_id,
                    discovered_from=DISCOVERED_FROM_CONTACT_LIST,
                    placeholder=True,
                )
                session.add(chatroom)
            chatroom.last_synced_at = synced_at

    async def _upsert_memberships(
        self,
        session: AsyncSession,
        *,
        chatroom: Chatroom,
        members_by_wxid: dict[str, ChatroomMember],
    ) -> int:
        memberships = list(
            await session.scalars(
                select(ChatroomMembership)
                .where(ChatroomMembership.chatroom_id == chatroom.id)
                .order_by(ChatroomMembership.membership_epoch.desc())
            )
        )
        active_by_wxid: dict[str, ChatroomMembership] = {}
        max_epoch_by_wxid: dict[str, int] = {}
        for membership in memberships:
            max_epoch_by_wxid[membership.member_wxid] = max(
                membership.membership_epoch,
                max_epoch_by_wxid.get(membership.member_wxid, 0),
            )
            if membership.left_at is None:
                active_by_wxid.setdefault(membership.member_wxid, membership)

        for member_wxid, member in members_by_wxid.items():
            current_membership = active_by_wxid.get(member_wxid)
            if current_membership is None:
                current_membership = ChatroomMembership(
                    chatroom_id=chatroom.id,
                    member_wxid=member_wxid,
                    membership_epoch=max_epoch_by_wxid.get(member_wxid, 0) + 1,
                )
                session.add(current_membership)
            self._update_membership(current_membership, member)

        return len(active_by_wxid.keys() - members_by_wxid.keys())

    @staticmethod
    def _update_membership(
        membership: ChatroomMembership,
        member: ChatroomMember,
    ) -> None:
        membership.nickname = member.nickname
        membership.display_name = member.display_name
        membership.inviter_wxid = member.inviter_wxid
        membership.member_flag = member.member_flag

    @staticmethod
    def _membership_view(membership: ChatroomMembership) -> MembershipView:
        return MembershipView(
            id=membership.id,
            chatroom_id=membership.chatroom_id,
            member_wxid=membership.member_wxid,
            membership_epoch=membership.membership_epoch,
            nickname=membership.nickname,
            display_name=membership.display_name,
            inviter_wxid=membership.inviter_wxid,
            member_flag=membership.member_flag,
            joined_at=membership.joined_at,
            left_at=membership.left_at,
            active=membership.left_at is None,
            created_at=membership.created_at,
            updated_at=membership.updated_at,
        )

    async def _account_connection(
        self,
        session: AsyncSession,
        bot_account_id: UUID,
    ) -> tuple[BotAccount, GeweConnection]:
        row = (
            await session.execute(
                select(BotAccount, GeweConnection)
                .join(
                    GeweConnection,
                    GeweConnection.id == BotAccount.gewe_connection_id,
                )
                .where(BotAccount.id == bot_account_id)
            )
        ).one_or_none()
        if row is None:
            raise DirectoryNotFoundError("bot account")
        return row[0], row[1]

    async def _chatroom_account_connection(
        self,
        session: AsyncSession,
        chatroom_uuid: UUID,
    ) -> tuple[Chatroom, BotAccount, GeweConnection]:
        row = (
            await session.execute(
                select(Chatroom, BotAccount, GeweConnection)
                .join(BotAccount, BotAccount.id == Chatroom.bot_account_id)
                .join(
                    GeweConnection,
                    GeweConnection.id == BotAccount.gewe_connection_id,
                )
                .where(Chatroom.id == chatroom_uuid)
            )
        ).one_or_none()
        if row is None:
            raise DirectoryNotFoundError("chatroom")
        return row[0], row[1], row[2]

    @staticmethod
    async def _require_account(session: AsyncSession, bot_account_id: UUID) -> None:
        account_id = await session.scalar(
            select(BotAccount.id).where(BotAccount.id == bot_account_id)
        )
        if account_id is None:
            raise DirectoryNotFoundError("bot account")

    @staticmethod
    async def _require_chatroom(session: AsyncSession, chatroom_uuid: UUID) -> None:
        chatroom_id = await session.scalar(select(Chatroom.id).where(Chatroom.id == chatroom_uuid))
        if chatroom_id is None:
            raise DirectoryNotFoundError("chatroom")

    def _decrypt_token(self, connection: GeweConnection) -> str:
        try:
            return self.cipher.decrypt(connection.token_ciphertext)
        except CredentialDecryptionError as exc:
            raise DirectoryCredentialError("connection credential unavailable") from exc
