"""PumpLens executable entry point. / Точка входа PumpLens."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pydantic import ValidationError

from pumplens import __version__
from pumplens.cli.scanner import run_scanner
from pumplens.config import RuntimeSecrets, load_settings
from pumplens.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pumplens",
        description="Binance Futures anomaly scanner / Сканер аномалий Binance Futures",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate-config",
        help="validate settings.yaml / проверить settings.yaml",
    )
    validate.add_argument("--settings", type=Path, default=Path("settings.yaml"))

    scan = subparsers.add_parser("scan", help="run Stage A scanner / запустить Stage A")
    scan.add_argument("--settings", type=Path, default=Path("settings.yaml"))
    scan.add_argument(
        "--duration",
        type=float,
        default=None,
        help="stop after N seconds; default runs forever / остановить через N секунд",
    )
    scan.add_argument(
        "--symbol-limit",
        type=int,
        default=None,
        help="development-only universe limit / лимит символов для разработки",
    )
    scan.add_argument("--display-seconds", type=float, default=5.0)
    scan.add_argument(
        "--record",
        type=Path,
        default=None,
        help="write normalized JSONL replay / записать JSONL для replay",
    )

    serve = subparsers.add_parser(
        "serve",
        help="run scanner, Telegram, and Mini App / запустить весь сервис",
    )
    serve.add_argument("--settings", type=Path, default=Path("settings.yaml"))
    return parser


def main() -> None:
    configure_logging()
    args = build_parser().parse_args()
    try:
        settings = load_settings(args.settings)
        if args.command == "validate-config":
            print("Configuration is valid / Конфигурация корректна")
            return
        if args.command == "scan":
            asyncio.run(
                run_scanner(
                    settings,
                    duration_seconds=args.duration,
                    symbol_limit=args.symbol_limit,
                    display_seconds=args.display_seconds,
                    record_path=args.record,
                )
            )
        if args.command == "serve":
            # Optional service dependencies are imported only for this command.
            # Опциональные зависимости сервиса импортируются только этой командой.
            from pumplens.application import run_full_service

            asyncio.run(run_full_service(settings, RuntimeSecrets()))
    except (FileNotFoundError, ValueError, ValidationError) as exc:
        raise SystemExit(f"Configuration error / Ошибка конфигурации: {exc}") from exc
    except KeyboardInterrupt:
        print("\nStopped / Остановлено")


if __name__ == "__main__":
    main()
