from typing import Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def get_currency_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="Юань (CNY)", callback_data="currency_CNY"),
            InlineKeyboardButton(text="Доллар (USD)", callback_data="currency_USD"),
        ],
        [
            InlineKeyboardButton(text="Евро (EUR)", callback_data="currency_EUR"),
            InlineKeyboardButton(text="Другая валюта", callback_data="currency_custom"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_bank_keyboard(banks: list[tuple]) -> InlineKeyboardMarkup:
    buttons = []
    for bank_id, name, is_configured in banks:
        status = " [OK]" if is_configured else " [Setup]"
        buttons.append(
            [InlineKeyboardButton(text=f"{name}{status}", callback_data=f"bank_{bank_id}")]
        )
    buttons.append(
        [InlineKeyboardButton(text="➕ Добавить свой банк", callback_data="bank_custom")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_interval_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="15 мин", callback_data="interval_900"),
            InlineKeyboardButton(text="30 мин", callback_data="interval_1800"),
        ],
        [
            InlineKeyboardButton(text="1 час", callback_data="interval_3600"),
            InlineKeyboardButton(text="3 часа", callback_data="interval_10800"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🔎 Проверить курс сейчас", callback_data="check_now")],
        [InlineKeyboardButton(text=f"Валюта: {settings['currency']}", callback_data="set_currency")],
        [InlineKeyboardButton(text=f"Банк: {settings['bank_name']}", callback_data="set_bank")],
        [InlineKeyboardButton(text=f"Порог: {settings['threshold']}", callback_data="set_threshold")],
        [InlineKeyboardButton(text=f"Шаг: {settings['step']}", callback_data="set_step")],
        [InlineKeyboardButton(text=f"Интервал: {settings['interval']} мин", callback_data="set_interval")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_setup")]]
    )


def get_captcha_keyboard(
    task_id: str, page_url: Optional[str] = None, has_image: bool = True
) -> InlineKeyboardMarkup:
    rows = []
    if has_image:
        rows.append([
            InlineKeyboardButton(
                text="🔄 Обновить картинку", callback_data=f"captcha_refresh_{task_id}"
            )
        ])
    else:
        if page_url:
            rows.append([InlineKeyboardButton(text="🌐 Открыть страницу", url=page_url)])
        rows.append([
            InlineKeyboardButton(
                text="🔄 Повторить проверку", callback_data=f"captcha_retry_{task_id}"
            )
        ])
    rows.append([
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"captcha_cancel_{task_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)