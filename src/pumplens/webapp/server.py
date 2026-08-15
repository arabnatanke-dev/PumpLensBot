"""Uvicorn lifecycle with query-safe logging. / Запуск Uvicorn без логов query string."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI


async def run_web_server(app: FastAPI, host: str, port: int) -> None:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    await uvicorn.Server(config).serve()
