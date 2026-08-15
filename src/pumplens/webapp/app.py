"""FastAPI Mini App and health endpoints. / FastAPI Mini App и health endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, text

from pumplens.binance.signed_rest import BinanceCredentialError, BinanceReadOnlyClient
from pumplens.config import RuntimeSecrets
from pumplens.onboarding.service import OnboardingService
from pumplens.security.credential_vault import CredentialVault
from pumplens.security.telegram_init_data import TelegramInitDataError, validate_init_data
from pumplens.service_state import ServiceState
from pumplens.storage.db import Database
from pumplens.storage.models import ExchangeAccountRecord, UserRecord
from pumplens.storage.repositories import CredentialRepository
from pumplens.webapp.sessions import ConnectSessionError, ConnectSessionStore

PACKAGE_DIR = Path(__file__).parent


class ConnectRequest(BaseModel):
    """SecretStr prevents accidental repr leakage. / SecretStr защищает repr от утечек."""

    model_config = ConfigDict(str_strip_whitespace=True)

    init_data: SecretStr
    session_token: SecretStr
    csrf_token: SecretStr
    api_key: SecretStr
    secret_key: SecretStr
    label: str = Field(default="Binance Main", min_length=1, max_length=128)
    read_only_confirmed: bool


class ConnectResponse(BaseModel):
    connected: bool
    key_last4: str
    message: str


def create_web_app(
    *,
    runtime: RuntimeSecrets,
    database: Database,
    connect_sessions: ConnectSessionStore,
    service_state: ServiceState,
) -> FastAPI:
    app = FastAPI(
        title="PumpLens Mini App",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self' https://telegram.org; "
            "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "form-action 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org"
        )
        return response

    @app.get("/connect", response_class=HTMLResponse)
    async def connect_page(
        request: Request,
        session: str = Query(min_length=20, max_length=128),
        csrf: str = Query(min_length=20, max_length=128),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "connect.html",
            {"session_token": session, "csrf_token": csrf},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/connect", response_model=ConnectResponse)
    async def connect_account(payload: ConnectRequest) -> ConnectResponse:
        token = _required_secret(runtime.telegram_bot_token, "telegram_not_configured")
        master_key = _required_secret(runtime.credential_master_key, "vault_not_configured")
        try:
            verified = validate_init_data(payload.init_data.get_secret_value(), token)
            connect_sessions.validate(
                payload.session_token.get_secret_value(),
                payload.csrf_token.get_secret_value(),
                verified.user_id,
            )
        except (TelegramInitDataError, ConnectSessionError) as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if not payload.read_only_confirmed:
            raise HTTPException(status_code=400, detail="read_only_confirmation_required")

        api_key = payload.api_key.get_secret_value()
        secret_key = payload.secret_key.get_secret_value()
        try:
            async with BinanceReadOnlyClient(api_key, secret_key) as binance:
                verification = await binance.verify_read_only()
        except BinanceCredentialError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Consume only after Binance accepted the key pair, so a typo does not burn the link.
        # Погашаем ссылку лишь после проверки Binance, чтобы опечатка её не сжигала.
        try:
            connect_sessions.consume(
                payload.session_token.get_secret_value(),
                payload.csrf_token.get_secret_value(),
                verified.user_id,
            )
        except ConnectSessionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        async with database.session() as db_session, db_session.begin():
            user = await db_session.scalar(
                select(UserRecord).where(UserRecord.telegram_user_id == verified.user_id)
            )
            if user is None:
                raise HTTPException(status_code=403, detail="user_not_registered")
            existing = await db_session.scalar(
                select(ExchangeAccountRecord).where(
                    ExchangeAccountRecord.user_id == user.id,
                    ExchangeAccountRecord.exchange == "BINANCE",
                )
            )
            account_id = existing.id if existing is not None else uuid.uuid4()
            encrypted = CredentialVault(master_key).encrypt(
                api_key,
                secret_key,
                exchange_account_id=str(account_id),
                telegram_user_id=verified.user_id,
            )
            await CredentialRepository(db_session).connect_binance(
                exchange_account_id=account_id,
                user_id=user.id,
                label=payload.label,
                permissions=verification.permissions,
                encrypted=encrypted,
            )
            OnboardingService.complete(user)
        return ConnectResponse(
            connected=True,
            key_last4=encrypted.api_key_last4,
            message="Read-only Binance connected / Read-only Binance подключён",
        )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness() -> dict[str, str]:
        if service_state.status().stale:
            raise HTTPException(status_code=503, detail="scanner_stale")
        try:
            async with database.session() as db_session:
                await db_session.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database_unavailable") from exc
        return {"status": "ready"}

    return app


def _required_secret(value: SecretStr | None, error: str) -> str:
    if value is None or not value.get_secret_value():
        raise HTTPException(status_code=503, detail=error)
    return value.get_secret_value()
