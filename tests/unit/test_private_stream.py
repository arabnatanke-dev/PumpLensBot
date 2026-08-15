"""Private stream authentication tests. / Тесты авторизации приватного потока."""

from unittest.mock import patch

from pumplens.binance.signed_rest import BinanceReadOnlyClient


async def test_spot_websocket_signature_is_hmac_and_deterministic() -> None:
    client = BinanceReadOnlyClient("api-key", "secret-key")
    try:
        with patch("pumplens.binance.signed_rest.time.time", return_value=1_700_000_000.0):
            params = client.signed_websocket_params()
        assert params == {
            "apiKey": "api-key",
            "timestamp": 1_700_000_000_000,
            "signature": "66473b208d61e31bf259d664794ff4139d619e50678763666baee89123edf95e",
        }
    finally:
        await client.aclose()
