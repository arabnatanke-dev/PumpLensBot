"""Stage A scoring and debounce tests. / Тесты score и debounce Stage A."""

from datetime import UTC, datetime

from pumplens.analytics.scoring import score_stage_a
from pumplens.analytics.stage_a import StageAScanner
from pumplens.config import LateSettings, ScannerSettings
from pumplens.domain.enums import DataQuality, Direction
from pumplens.domain.models import FeatureSnapshot


def strong_snapshot() -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="TESTUSDT",
        timestamp=datetime.now(UTC),
        direction=Direction.LONG,
        last_price=1.02,
        return_1m=1.2,
        return_3m=1.8,
        return_5m=2.0,
        return_15m=2.5,
        acceleration_1m=0.7,
        volume_ratio_1m=5.0,
        volume_robust_z=6.0,
        buy_pressure=0.68,
        trade_rate_ratio=4.0,
        spread_pct=0.05,
        relative_strength_1m=1.0,
        range_pct_1m=2.0,
        candle_structure=0.9,
        quote_volume_24h=10_000_000,
        data_quality=DataQuality.FRESH,
    )


class StaticFeatureEngine:
    """Minimal deterministic engine double. / Минимальный детерминированный double."""

    def snapshot_all(self) -> list[FeatureSnapshot]:
        return [strong_snapshot()]


def test_strong_snapshot_has_explainable_score() -> None:
    result = score_stage_a(strong_snapshot(), max_spread_pct=0.35)
    assert result.score >= 55
    assert any(reason.startswith("volume") for reason in result.reasons)
    assert any(reason.startswith("pressure") for reason in result.reasons)


def test_candidate_requires_two_hits_out_of_three() -> None:
    scanner = StageAScanner(  # type: ignore[arg-type]
        StaticFeatureEngine(),
        ScannerSettings(),
        LateSettings(),
    )
    first = scanner.scan_once()[0]
    second = scanner.scan_once()[0]
    assert first.selected is False
    assert second.selected is True
