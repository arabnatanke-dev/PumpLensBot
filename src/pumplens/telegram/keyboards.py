"""Inline onboarding keyboards. / Inline-клавиатуры регистрации."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo


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
