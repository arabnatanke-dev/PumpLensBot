"""Mini App security-header tests. / Тесты security headers Mini App."""

from fastapi.testclient import TestClient

from pumplens.config import RuntimeSecrets
from pumplens.service_state import ServiceState
from pumplens.storage.db import Database
from pumplens.webapp.app import create_web_app
from pumplens.webapp.sessions import ConnectSessionStore


def test_connect_page_has_no_store_and_csp() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    app = create_web_app(
        runtime=RuntimeSecrets(_env_file=None),
        database=database,
        connect_sessions=ConnectSessionStore(),
        service_state=ServiceState(),
    )
    with TestClient(app) as client:
        response = client.get(
            "/connect",
            params={"session": "s" * 32, "csrf": "c" * 32},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"].startswith("no-store")
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert "localStorage" not in response.text
        assert client.get("/health/live").json() == {"status": "ok"}


def test_connect_rejects_invalid_telegram_data_before_binance_call() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    runtime = RuntimeSecrets(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="123:token",
        CREDENTIAL_MASTER_KEY="eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=",
    )
    app = create_web_app(
        runtime=runtime,
        database=database,
        connect_sessions=ConnectSessionStore(),
        service_state=ServiceState(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/connect",
            json={
                "init_data": "invalid=true",
                "session_token": "s" * 32,
                "csrf_token": "c" * 32,
                "api_key": "key",
                "secret_key": "secret",
                "label": "Binance Main",
                "read_only_confirmed": True,
            },
        )
        assert response.status_code == 401
