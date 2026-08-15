"""Configuration contract tests. / Тесты контракта конфигурации."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from pumplens.config import AppSettings, ScannerSettings, load_settings


def test_repository_settings_are_valid() -> None:
    root = Path(__file__).parents[2]
    settings = load_settings(root / "settings.yaml")
    assert settings.scanner.candidate_score == 55
    assert settings.early.shadow_mode is True
    assert settings.early.debounce_hits == 2
    assert settings.binance.websocket_streams_per_connection <= 900
    assert settings.telegram.signal_ttl_hours == 47
    assert settings.telegram.cleanup_scan_seconds == 60


def test_score_thresholds_must_be_monotonic() -> None:
    with pytest.raises(ValidationError):
        ScannerSettings(candidate_score=80, watch_score=70, confirmed_score=82)


def test_unknown_settings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"scanner": {"unknown_typo": 1}})
