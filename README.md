# PumpLens

PumpLens is an explainable anomaly scanner for Binance USDⓈ-M perpetual futures.
PumpLens — объяснимый сканер аномалий бессрочных USDⓈ-M фьючерсов Binance.

The repository contains the public scanner, Stage B, bounded Stage C entry validation,
signal FSM, Telegram onboarding and delivery, PostgreSQL persistence, protected Mini App,
encrypted read-only Binance
connections, portfolio reconciliation, replay, and Docker deployment. No Binance or
Telegram key is needed to run the standalone market scanner.

В репозитории есть публичный сканер, Stage B, ограниченный Stage C, FSM сигналов,
Telegram onboarding и доставка, PostgreSQL, защищённая Mini App, зашифрованное read-only
подключение Binance,
портфель, replay и Docker-развёртывание. Для отдельного рыночного CLI-сканера ключи не
нужны.

## Quick start / Быстрый запуск

```bash
cp .env.example .env
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,telegram,storage,security,webapp]'
pumplens validate-config
pumplens scan
```

Useful development commands / Команды разработки:

```bash
pytest
ruff check .
mypy src
```

Record normalized data for deterministic replay / Запись данных для replay:

```bash
pumplens scan --record data/market-$(date +%Y%m%d).jsonl
```

## Secrets / Секреты

Copy `.env.example` to `.env` and fill these values yourself / Скопируйте `.env.example`
в `.env` и заполните самостоятельно:

- `TELEGRAM_BOT_TOKEN` — token from BotFather / токен от BotFather;
- `DATABASE_URL` and `POSTGRES_PASSWORD` — PostgreSQL credentials;
- `CREDENTIAL_MASTER_KEY` — exactly 32 random bytes encoded as base64;
- `PUBLIC_BASE_URL` and `PUBLIC_HOST` — the HTTPS Mini App domain.

Generate a master key locally / Создать мастер-ключ локально:

```bash
openssl rand -base64 32
```

Never paste a user Binance API key into `.env`. Each user adds it only through the
Mini App. PumpLens rejects keys with trading, withdrawals, margin, transfers, Futures
trading, or portfolio-margin permissions.

Никогда не добавляйте пользовательский Binance API-ключ в `.env`. Пользователь вводит
его только в Mini App. PumpLens отклоняет ключи с торговлей, выводом, margin, transfers,
Futures trading или portfolio-margin permissions.

## Full service / Полный сервис

Local / Локально:

```bash
alembic upgrade head
pumplens serve
```

Docker with PostgreSQL, migrations, TLS proxy, health checks, and restart policies /
Docker с PostgreSQL, миграциями, TLS, health checks и restart policy:

```bash
docker compose up --build -d
docker compose ps
```

`POSTGRES_PASSWORD` is mandatory; Docker refuses `change_me` and the example placeholder.
`POSTGRES_PASSWORD` обязателен; Docker отклоняет `change_me` и пример-заглушку.

Binance USD-M public streams use separate routed connections: regular market data through
`/market`, and book/depth data through `/public`. Connected portfolios use the current
Futures `/private` user stream and signed Spot WebSocket API. Private events trigger a
debounced full read-only reconciliation; no order-creation method exists.
Публичные потоки Binance USD-M разделены: обычные рыночные данные идут через `/market`,
а book/depth — через `/public`. Подключённые портфели используют актуальный Futures
`/private` и подписанный Spot WebSocket API. Приватные события запускают полную
read-only сверку с debounce; методов создания ордеров в проекте нет.

Run the opt-in live WebSocket check with `make smoke-ws`.
Живую WebSocket-проверку можно запустить командой `make smoke-ws`.

## Security / Безопасность

- Never commit `.env`, API keys, Telegram tokens, or the credential master key.
- Никогда не добавляйте в git `.env`, API-ключи, Telegram-токены и мастер-ключ.
- The market scanner uses only public Binance endpoints.
- Рыночный сканер использует только публичные endpoint Binance.
- User exchange keys will be accepted only through the protected Mini App and stored
  encrypted; they will not be read from this repository's environment file.
- Ключи пользователей будут приниматься только через защищённую Mini App и храниться
  в зашифрованном виде; они не будут читаться из `.env` этого репозитория.

## Status / Состояние

Implemented / Реализовано:

- dynamic USDⓈ-M perpetual universe;
- REST warmup and rate limiting;
- resilient public WebSocket ingestion;
- ring buffers and deterministic features;
- Stage A score and CLI top candidates;
- dynamic trade/depth Stage B and OI polling;
- bounded Stage C market structure, support/resistance zones, breakout/retest,
  ATR/VWAP late detection, exhaustion, hypothetical R:R, and Entry Quality;
- debounced signal FSM and human-readable levels;
- Telegram invite onboarding, top/status/portfolio, and persistent delivery queue;
- real-time Spot/Futures private account monitoring with periodic REST reconciliation;
- persistent portfolio alerts: match, opposite position, margin, PnL, and stale data;
- `/binance`, `/portfolio`, `/positions`, `/history`, `/stats`, and deterministic
  `/why SYMBOL` Telegram flows;
- EARLY anomaly radar with liquidity-aware activity floors, short rearm, shadow
  persistence, replay outcomes, and `/early_stats`;
- mobile Binance app bridge with a Web fallback;
- PostgreSQL schema and Alembic migration;
- AES-256-GCM credential vault and fail-closed read-only permission checks;
- Spot/Futures portfolio reconciliation, continuous JSONL replay, and 5/15/60m outcomes;
- unit, security, FSM, storage, portfolio, and replay tests.

Before production / Перед production: enter your secrets, configure the public DNS,
run the migration, and collect 7–14 days of replay data to tune thresholds. Trading
order creation is deliberately absent from the codebase.

`early.shadow_mode: true` is the safe default: EARLY transitions and their full feature
snapshots are stored, but Telegram deliveries are not created. Review 3–7 days through
`/early_stats` and replay before enabling user notifications.

`early.shadow_mode: true` — безопасное значение по умолчанию: переходы EARLY и полные
feature snapshot сохраняются, но Telegram delivery не создаются. Перед включением
уведомлений соберите 3–7 дней и проверьте `/early_stats` вместе с replay.
