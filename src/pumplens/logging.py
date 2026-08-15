"""Structured logging setup. / Настройка структурированного логирования."""

from __future__ import annotations

import logging
import os
import sys

import structlog

from pumplens.security.redaction import structlog_redactor


def configure_logging() -> None:
    """Configure JSON in production and readable console logs locally. / Настраивает логи."""

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    production = os.getenv("APP_ENV", "development").lower() == "production"

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    # Signed Binance query strings contain signatures; never let HTTP clients print URLs.
    # Подписанные Binance URL содержат signature; запрещаем HTTP-клиенту печатать их.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    renderer = (
        structlog.processors.JSONRenderer() if production else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog_redactor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
