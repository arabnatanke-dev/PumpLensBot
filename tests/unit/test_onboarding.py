"""Invite and Mini App session tests. / Тесты invite и сессий Mini App."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from pumplens.onboarding.invites import InviteError, InviteService
from pumplens.onboarding.service import OnboardingService
from pumplens.storage.db import Database
from pumplens.storage.models import Base, InviteCodeRecord, UserRecord
from pumplens.webapp.sessions import ConnectSessionError, ConnectSessionStore


async def make_database() -> Database:
    database = Database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database


async def test_invite_is_hashed_and_single_use() -> None:
    database = await make_database()
    invites = InviteService()
    try:
        async with database.session() as session, session.begin():
            raw_code = await invites.create(session, created_by=None, max_uses=1)
        async with database.session() as session, session.begin():
            record = await session.scalar(select(InviteCodeRecord))
            assert record is not None
            assert record.code_hash != raw_code
            await invites.redeem(session, raw_code)
        async with database.session() as session, session.begin():
            with pytest.raises(InviteError):
                await invites.redeem(session, raw_code)
    finally:
        await database.dispose()


async def test_onboarding_start_is_idempotent_after_invite() -> None:
    database = await make_database()
    invites = InviteService()
    service = OnboardingService(invites)
    try:
        async with database.session() as session, session.begin():
            raw_code = await invites.create(session, created_by=None)
        async with database.session() as session, session.begin():
            first = await service.start(
                session,
                telegram_user_id=42,
                chat_id=42,
                display_name="Rose",
                language="ru",
                invite_code=raw_code,
            )
            first_id = first.id
        async with database.session() as session, session.begin():
            second = await service.start(
                session,
                telegram_user_id=42,
                chat_id=43,
                display_name="Rose",
                language="ru",
                invite_code=None,
            )
            assert second.id == first_id
        async with database.session() as session:
            users = list((await session.scalars(select(UserRecord))).all())
            assert len(users) == 1
    finally:
        await database.dispose()


def test_connect_session_is_bound_and_one_time() -> None:
    store = ConnectSessionStore(ttl_seconds=600)
    tokens = store.create(telegram_user_id=42)
    with pytest.raises(ConnectSessionError):
        store.consume(tokens.session_token, tokens.csrf_token, telegram_user_id=43)

    replacement = store.create(telegram_user_id=42)
    store.consume(replacement.session_token, replacement.csrf_token, telegram_user_id=42)
    with pytest.raises(ConnectSessionError):
        store.consume(replacement.session_token, replacement.csrf_token, telegram_user_id=42)


def test_connect_session_validation_does_not_consume_link() -> None:
    store = ConnectSessionStore(ttl_seconds=600)
    tokens = store.create(telegram_user_id=42)

    store.validate(tokens.session_token, tokens.csrf_token, telegram_user_id=42)
    store.validate(tokens.session_token, tokens.csrf_token, telegram_user_id=42)
    store.consume(tokens.session_token, tokens.csrf_token, telegram_user_id=42)

    with pytest.raises(ConnectSessionError):
        store.validate(tokens.session_token, tokens.csrf_token, telegram_user_id=42)


async def test_expired_invite_is_rejected() -> None:
    database = await make_database()
    try:
        raw_code = "expired-code"
        async with database.session() as session, session.begin():
            session.add(
                InviteCodeRecord(
                    code_hash=InviteService.hash_code(raw_code),
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    max_uses=1,
                )
            )
        async with database.session() as session, session.begin():
            with pytest.raises(InviteError):
                await InviteService().redeem(session, raw_code)
    finally:
        await database.dispose()
