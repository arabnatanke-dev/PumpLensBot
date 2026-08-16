"""Inline onboarding keyboards. / Inline-клавиатуры регистрации."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)


def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Начать", callback_data="onboarding:begin")],
            [InlineKeyboardButton(text="Что умеет бот?", callback_data="onboarding:about")],
        ]
    )


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Принимаю", callback_data="onboarding:consent")],
            [InlineKeyboardButton(text="Выйти", callback_data="onboarding:exit")],
        ]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="SAFE — реже", callback_data="profile:safe")],
            [
                InlineKeyboardButton(
                    text="BALANCED — рекомендуемый",
                    callback_data="profile:balanced",
                )
            ],
            [InlineKeyboardButton(text="WILD — чаще", callback_data="profile:wild")],
        ]
    )


def directions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="LONG + SHORT", callback_data="directions:both")],
            [
                InlineKeyboardButton(text="Только LONG", callback_data="directions:long"),
                InlineKeyboardButton(text="Только SHORT", callback_data="directions:short"),
            ],
        ]
    )


def binance_keyboard(connect_url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if connect_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Подключить портфель",
                    web_app=WebAppInfo(url=connect_url),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Позже", callback_data="binance:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def connected_binance_keyboard(connect_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💼 Портфель", callback_data="binance:portfolio"),
            InlineKeyboardButton(text="📊 Позиции", callback_data="binance:positions"),
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="binance:refresh")],
    ]
    if connect_url:
        rows.append(
            [InlineKeyboardButton(text="Заменить API-ключ", web_app=WebAppInfo(url=connect_url))]
        )
    rows.append(
        [InlineKeyboardButton(text="Отключить Binance", callback_data="binance:disconnect")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def disconnect_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отключить",
                    callback_data="binance:disconnect_confirm",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="binance:disconnect_cancel")],
        ]
    )


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="💼 Портфель"),
                KeyboardButton(text="📈 Сигналы"),
            ],
            [
                KeyboardButton(text="📊 Позиции"),
                KeyboardButton(text="🧪 EARLY"),
            ],
            [
                KeyboardButton(text="⚙️ Настройки"),
                KeyboardButton(text="🔗 Binance"),
            ],
        ],
        is_persistent=True,
        resize_keyboard=True,
        input_field_placeholder="Выберите раздел PumpLens",
    )


def panel_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")],
        ]
    )


def portfolio_keyboard(active: str = "overview") -> InlineKeyboardMarkup:
    """Portfolio tabs edit one content screen. / Вкладки редактируют одну карточку."""

    def label(key: str, text: str) -> str:
        return f"• {text}" if active == key else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label("overview", "Обзор"),
                    callback_data="screen:portfolio:overview",
                ),
                InlineKeyboardButton(
                    text=label("spot", "Spot"),
                    callback_data="screen:portfolio:spot",
                ),
                InlineKeyboardButton(
                    text=label("futures", "Futures"),
                    callback_data="screen:portfolio:futures",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=label("earn", "Earn"),
                    callback_data="screen:portfolio:earn",
                ),
                InlineKeyboardButton(
                    text=label("funding", "Funding"),
                    callback_data="screen:portfolio:funding",
                ),
            ],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="screen:refresh:portfolio")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")],
        ]
    )


def signals_keyboard(direction: str = "all") -> InlineKeyboardMarkup:
    def label(key: str, text: str) -> str:
        return f"• {text}" if direction == key else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label("all", "Все"), callback_data="screen:signals:all"
                ),
                InlineKeyboardButton(
                    text=label("long", "LONG"), callback_data="screen:signals:long"
                ),
                InlineKeyboardButton(
                    text=label("short", "SHORT"), callback_data="screen:signals:short"
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")],
        ]
    )


def early_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="screen:early:refresh")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")],
        ]
    )


def panel_settings_keyboard(
    *,
    profile: str,
    directions: list[str],
    connected: bool,
    connect_url: str | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if profile == 'safe' else ''}SAFE",
                callback_data="panel:profile:safe",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if profile == 'balanced' else ''}BALANCED",
                callback_data="panel:profile:balanced",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if profile == 'wild' else ''}WILD",
                callback_data="panel:profile:wild",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if sorted(directions) == ['LONG', 'SHORT'] else ''}LONG + SHORT",
                callback_data="panel:directions:both",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if directions == ['LONG'] else ''}LONG",
                callback_data="panel:directions:long",
            ),
            InlineKeyboardButton(
                text=f"{'✓ ' if directions == ['SHORT'] else ''}SHORT",
                callback_data="panel:directions:short",
            ),
        ],
    ]
    if connect_url:
        label = "🔄 Переподключить Binance" if connected else "🔗 Подключить Binance"
        rows.append([InlineKeyboardButton(text=label, web_app=WebAppInfo(url=connect_url))])
    if connected:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Синхронизировать",
                    callback_data="screen:refresh:binance",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отключить Binance",
                    callback_data="binance:disconnect",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def panel_binance_keyboard(
    *,
    connected: bool,
    connect_url: str | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if connect_url:
        label = "🔄 Переподключить Binance" if connected else "🔗 Подключить Binance"
        rows.append([InlineKeyboardButton(text=label, web_app=WebAppInfo(url=connect_url))])
    if connected:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Синхронизировать",
                    callback_data="screen:refresh:binance",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отключить Binance",
                    callback_data="binance:disconnect",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
