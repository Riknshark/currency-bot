import asyncio
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot.keyboards import (
    get_bank_keyboard,
    get_cancel_keyboard,
    get_captcha_keyboard,
    get_currency_keyboard,
    get_interval_keyboard,
    get_settings_keyboard,
)
from bot.states import SetupStates
from config import MAX_FETCH_ERRORS
from database.manager import DatabaseManager
from services.bank_parser import FetchResult, ParserFactory
from services.captcha_bridge import (
    CaptchaChallenge,
    CaptchaManager,
    CaptchaSolver,
    PendingCaptchaTask,
)

router = Router()
logger = logging.getLogger(__name__)

NO_API_PARSER_TYPES = (
    "sber",
    "alfa",
    "tbank",
    "atb",
    "ingo_page",
    "ingobank",
    "moex",
    "cbrf",
    "cbrf_json",
)


class HasPendingCaptchaFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user or not message.text:
            return False
        if message.text.strip().startswith("/"):
            return False
        return CaptchaManager.has_pending(message.from_user.id)


def _parse_positive_float(text: str):
    try:
        value = float(text.replace(",", ".").strip())
    except ValueError:
        return None
    return value if value > 0 else None


async def _reply(event, text: str, reply_markup=None):
    if isinstance(event, CallbackQuery):
        await event.message.answer(text, reply_markup=reply_markup)
    else:
        await event.answer(text, reply_markup=reply_markup)


async def _show_settings_menu(event, state: FSMContext):
    settings = await DatabaseManager.get_user_settings(event.from_user.id)
    if not settings:
        await _reply(event, "Активная подписка не найдена. Используйте /start для настройки.")
        return
    currency, bank_id, threshold, step, interval_sec = settings
    details = await DatabaseManager.get_bank_details(bank_id)
    bank_name = details[3] if details else "Неизвестно"
    settings_dict = {
        "currency": currency,
        "bank_name": bank_name,
        "threshold": threshold,
        "step": step,
        "interval": interval_sec // 60,
    }
    text = (
        "Текущие настройки:\n"
        f"Валюта: {currency}\n"
        f"Банк: {bank_name}\n"
        f"Порог: {threshold}\n"
        f"Шаг: {step}\n"
        f"Интервал: {interval_sec // 60} мин\n\n"
        "Выберите параметр для изменения:"
    )
    await _reply(event, text, get_settings_keyboard(settings_dict))


async def _send_captcha_to_user(
    bot: Bot,
    user_id: int,
    bank_id: str,
    bank_name: str,
    currency: str,
    threshold: float,
    step: float,
    challenge: CaptchaChallenge,
    prefix: str = "",
    force: bool = False,
):
    if not force and not CaptchaManager.should_prompt(user_id, challenge.domain, force=False):
        return

    existing = CaptchaManager.get_pending(user_id)
    if existing and existing.challenge.domain == challenge.domain and existing.currency == currency:
        task = CaptchaManager.update_challenge(user_id, challenge)
    else:
        task = CaptchaManager.create_task(
            user_id=user_id,
            bank_id=bank_id,
            bank_name=bank_name,
            currency=currency,
            threshold=threshold,
            step=step,
            challenge=challenge,
        )

    if challenge.image_bytes:
        caption = (
            f"{prefix}\n" if prefix else ""
        ) + (
            f"🤖 {bank_name} ({challenge.domain}) запросил капчу ({challenge.reason}) "
            f"при проверке курса {currency}.\n"
            "Введите символы с картинки ответным сообщением:"
        )
        photo = BufferedInputFile(challenge.image_bytes, filename="captcha.png")
        await bot.send_photo(
            chat_id=user_id,
            photo=photo,
            caption=caption,
            reply_markup=get_captcha_keyboard(task.task_id, challenge.page_url, has_image=True),
        )
    else:
        text = (
            f"{prefix}\n\n" if prefix else ""
        ) + (
            f"🛡 {bank_name} ({challenge.domain}) включил защиту ({challenge.reason}) "
            f"при запросе курса {currency}.\n\n"
            "Откройте страницу в браузере, пройдите проверку и:\n"
            "• отправьте сюда строку Cookie из браузера (например `cf_clearance=...; ...`), или\n"
            "• нажмите «Повторить проверку»."
        )
        await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=get_captcha_keyboard(task.task_id, challenge.page_url, has_image=False),
        )


async def _run_immediate_check(bot: Bot, user_id: int):
    row = await DatabaseManager.get_user_full(user_id)
    if not row:
        await bot.send_message(user_id, "Подписка не найдена. Используйте /start.")
        return

    _, currency, bank_id, threshold, step, last_notified_rate, _, _, _ = row
    details = await DatabaseManager.get_bank_details(bank_id)
    if not details:
        await bot.send_message(user_id, "Банк не найден в базе.")
        return

    api_url, json_path, api_key, bank_name, parser_type = details
    parser = ParserFactory.get_parser(parser_type, api_url or "", json_path or "", api_key)
    if not parser:
        await bot.send_message(user_id, f"Парсер для {bank_name} не настроен.")
        return

    res: FetchResult = await parser.fetch_rate_detailed(currency)
    if res.rate is not None:
        await DatabaseManager.update_user_field(user_id, "error_count", 0)
        src = f" ({res.source})" if res.source else ""
        await bot.send_message(
            user_id,
            f"💱 Курс {currency} в {bank_name}{src}: {res.rate:.4f}".rstrip("0").rstrip("."),
        )
        if last_notified_rate is None and res.rate <= threshold:
            await DatabaseManager.update_user_field(user_id, "last_notified_rate", res.rate)
        return

    if res.captcha:
        await _send_captcha_to_user(
            bot=bot,
            user_id=user_id,
            bank_id=bank_id,
            bank_name=bank_name,
            currency=currency,
            threshold=threshold,
            step=step,
            challenge=res.captcha,
            force=True,
        )
        return

    err_text = res.error or "неизвестная ошибка"
    await bot.send_message(
        user_id,
        f"Не удалось получить курс {currency} из {bank_name}: {err_text}",
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Добро пожаловать в трекер курсов валют.\n"
        "Бот отслеживает курс и присылает уведомления по вашему порогу и шагу.\n\n"
        "Выберите валюту для отслеживания:",
        reply_markup=get_currency_keyboard(),
    )
    await state.set_state(SetupStates.choosing_currency)


@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel_setup")
async def cmd_cancel(event, state: FSMContext):
    await state.clear()
    CaptchaManager.clear(event.from_user.id)
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer("Действие отменено. Отправьте /start или /settings.")
    else:
        await event.answer("Действие отменено. Отправьте /start или /settings.")


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    await state.clear()
    await _show_settings_menu(message, state)


@router.message(Command("check"))
async def cmd_check(message: Message, bot: Bot):
    await message.answer("Проверяю актуальный курс...")
    await _run_immediate_check(bot, message.from_user.id)


@router.callback_query(F.data == "check_now")
async def cb_check_now(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await callback.message.answer("Проверяю актуальный курс...")
    await _run_immediate_check(bot, callback.from_user.id)


# === Human-in-the-loop CAPTCHA handlers ===

@router.message(HasPendingCaptchaFilter(), F.text)
async def process_captcha_answer(message: Message, bot: Bot):
    user_id = message.from_user.id
    task: PendingCaptchaTask | None = CaptchaManager.get_pending(user_id)
    if not task:
        return

    ok, new_challenge, err_msg = await CaptchaSolver.submit_solution(
        task.challenge, message.text
    )
    if not ok:
        if new_challenge:
            await _send_captcha_to_user(
                bot=bot,
                user_id=user_id,
                bank_id=task.bank_id,
                bank_name=task.bank_name,
                currency=task.currency,
                threshold=task.threshold,
                step=task.step,
                challenge=new_challenge,
                prefix=err_msg or "Попробуйте ещё раз:",
                force=True,
            )
        else:
            await message.answer(
                f"{err_msg or 'Не удалось пройти проверку.'}\n"
                "Попробуйте снова или нажмите /cancel."
            )
        return

    # Captcha solved! Immediately retry fetching rate
    CaptchaManager.clear(user_id)
    details = await DatabaseManager.get_bank_details(task.bank_id)
    if not details:
        await message.answer("Капча пройдена, но банк не найден в настройках.")
        return

    api_url, json_path, api_key, bank_name, parser_type = details
    parser = ParserFactory.get_parser(parser_type, api_url or "", json_path or "", api_key)
    if not parser:
        await message.answer("Капча пройдена.")
        return

    res = await parser.fetch_rate_detailed(task.currency)
    if res.rate is not None:
        await DatabaseManager.update_user_field(user_id, "error_count", 0)
        await message.answer(
            f"✅ Капча пройдена!\n"
            f"Курс {task.currency} в {bank_name}: {res.rate}"
        )
        user_row = await DatabaseManager.get_user_full(user_id)
        if user_row:
            last_notified_rate = user_row[5]
            if last_notified_rate is None and res.rate <= task.threshold:
                await DatabaseManager.update_user_field(user_id, "last_notified_rate", res.rate)
            elif last_notified_rate is not None and abs(res.rate - last_notified_rate) >= task.step:
                await DatabaseManager.update_user_field(user_id, "last_notified_rate", res.rate)
        return

    if res.captcha:
        await _send_captcha_to_user(
            bot=bot,
            user_id=user_id,
            bank_id=task.bank_id,
            bank_name=bank_name,
            currency=task.currency,
            threshold=task.threshold,
            step=task.step,
            challenge=res.captcha,
            prefix="Сайт запросил дополнительную проверку:",
            force=True,
        )
        return

    await message.answer(
        f"Капча пройдена, но курс пока не получен ({res.error or 'ошибка источника'})."
    )


@router.callback_query(F.data.startswith("captcha_cancel_"))
async def cb_captcha_cancel(callback: CallbackQuery):
    await callback.answer()
    CaptchaManager.clear(callback.from_user.id)
    await callback.message.answer("Проверка капчи отменена. Мониторинг продолжится по расписанию.")


@router.callback_query(F.data.startswith("captcha_refresh_") | F.data.startswith("captcha_retry_"))
async def cb_captcha_retry(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    task_id = callback.data.rsplit("_", 1)[-1]
    task = CaptchaManager.get_by_id(task_id) or CaptchaManager.get_pending(callback.from_user.id)
    if not task:
        await callback.message.answer("Задача капчи устарела. Используйте /check для новой проверки.")
        return

    details = await DatabaseManager.get_bank_details(task.bank_id)
    if not details:
        CaptchaManager.clear(callback.from_user.id)
        return

    api_url, json_path, api_key, bank_name, parser_type = details
    parser = ParserFactory.get_parser(parser_type, api_url or "", json_path or "", api_key)
    if not parser:
        return

    res = await parser.fetch_rate_detailed(task.currency)
    if res.rate is not None:
        CaptchaManager.clear(callback.from_user.id)
        await DatabaseManager.update_user_field(callback.from_user.id, "error_count", 0)
        await callback.message.answer(
            f"✅ Проверка пройдена! Курс {task.currency} в {bank_name}: {res.rate}"
        )
        return

    if res.captcha:
        await _send_captcha_to_user(
            bot=bot,
            user_id=callback.from_user.id,
            bank_id=task.bank_id,
            bank_name=bank_name,
            currency=task.currency,
            threshold=task.threshold,
            step=task.step,
            challenge=res.captcha,
            prefix="Обновлённая проверка:",
            force=True,
        )
    else:
        await callback.message.answer(
            f"Не удалось получить курс: {res.error or 'неизвестная ошибка'}"
        )


# === Settings & Onboarding ===

@router.callback_query(F.data.startswith("set_"))
async def process_settings_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    param = callback.data.split("_", 1)[1]
    await state.update_data(is_editing=True)
    if param == "currency":
        await callback.message.answer("Выберите новую валюту:", reply_markup=get_currency_keyboard())
        await state.set_state(SetupStates.choosing_currency)
    elif param == "bank":
        banks = await DatabaseManager.get_all_banks()
        await callback.message.answer("Выберите новый банк:", reply_markup=get_bank_keyboard(banks))
        await state.set_state(SetupStates.choosing_bank)
    elif param == "threshold":
        await callback.message.answer("Введите новый порог (например, 12.50):")
        await state.set_state(SetupStates.entering_threshold)
    elif param == "step":
        await callback.message.answer("Введите новый шаг уведомлений (например, 0.10):")
        await state.set_state(SetupStates.entering_step)
    elif param == "interval":
        await callback.message.answer(
            "Выберите новый интервал проверки:", reply_markup=get_interval_keyboard()
        )
        await state.set_state(SetupStates.choosing_interval)


@router.callback_query(SetupStates.choosing_currency, F.data.startswith("currency_"))
async def process_currency(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    code = callback.data.split("_", 1)[1]
    if code == "custom":
        await callback.message.answer("Введите 3-буквенный код валюты (например: USD, EUR, GBP):")
        return
    await _apply_currency(callback, code.upper(), state)


@router.message(SetupStates.choosing_currency, F.text)
async def process_custom_currency(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    if len(code) != 3 or not code.isalpha():
        await message.answer("Неверный код. Введите ровно 3 латинские буквы, например CNY или USD.")
        return
    await _apply_currency(message, code, state)


async def _apply_currency(event, currency: str, state: FSMContext):
    data = await state.get_data()
    if data.get("is_editing"):
        await DatabaseManager.update_user_field(event.from_user.id, "currency", currency)
        await DatabaseManager.update_user_field(event.from_user.id, "last_notified_rate", None)
        await state.clear()
        await _reply(event, f"Валюта обновлена на {currency}.")
        await _show_settings_menu(event, state)
        return
    await state.update_data(currency=currency, is_editing=False)
    banks = await DatabaseManager.get_all_banks()
    await _reply(
        event,
        f"Выбрана валюта {currency}. Выберите банк для мониторинга:",
        get_bank_keyboard(banks),
    )
    await state.set_state(SetupStates.choosing_bank)


@router.callback_query(SetupStates.choosing_bank, F.data.startswith("bank_"))
async def process_bank(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bank_id = callback.data.split("_", 1)[1]
    if bank_id == "custom":
        await callback.message.answer("Введите название своего банка (от 1 до 40 символов):")
        await state.set_state(SetupStates.entering_custom_bank_name)
        return
    await state.update_data(bank_id=bank_id)
    details = await DatabaseManager.get_bank_details(bank_id)
    parser_type = details[4] if details else None
    configured = bool(details) and (bool(details[0]) or parser_type in NO_API_PARSER_TYPES)
    data = await state.get_data()
    if data.get("is_editing") and configured:
        await DatabaseManager.update_user_field(callback.from_user.id, "bank_id", bank_id)
        await DatabaseManager.update_user_field(callback.from_user.id, "last_notified_rate", None)
        await state.clear()
        await callback.message.answer("Банк обновлён.")
        await _show_settings_menu(callback, state)
        return
    if configured:
        await _proceed_to_threshold(callback, state)
    else:
        await _start_api_wizard(callback, state)


@router.message(SetupStates.entering_custom_bank_name, F.text)
async def process_custom_bank_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) > 40:
        await message.answer("Введите название длиной от 1 до 40 символов.")
        return
    slug = re.sub(r"[^a-z0-9]", "", name.lower())[:20] or str(int(time.time()))[-6:]
    bank_id = f"custom_{slug}"
    await DatabaseManager.add_custom_bank(bank_id, name)
    await state.update_data(bank_id=bank_id)
    await _start_api_wizard(message, state)


async def _start_api_wizard(event, state: FSMContext):
    guide = (
        "Для этого банка требуется ручная настройка API.\n\n"
        "ШАГ 1: Найдите URL API\n"
        "1. Откройте сайт банка в браузере.\n"
        "2. Нажмите F12 → Network → Fetch/XHR.\n"
        "3. Обновите страницу и найдите JSON-запрос с курсами валют.\n"
        "4. Скопируйте Request URL (код валюты можно заменить на {currency}).\n\n"
        "Отправьте URL API:"
    )
    await _reply(event, guide, get_cancel_keyboard())
    await state.set_state(SetupStates.entering_api_url)


@router.message(SetupStates.entering_api_url, F.text.startswith("http"))
async def process_api_url(message: Message, state: FSMContext):
    await state.update_data(api_url=message.text.strip())
    await message.answer(
        "ШАГ 2: Путь в JSON\n"
        "Укажите путь к значению курса через точку, например data.rates.CNY или data.0.currRate.\n"
        "Можно использовать {currency}.\n\n"
        "Отправьте JSON-путь (или напишите 'none'):"
    )
    await state.set_state(SetupStates.entering_json_path)


@router.message(SetupStates.entering_api_url)
async def process_invalid_api_url(message: Message):
    await message.answer("URL должен начинаться с http:// или https://. Попробуйте снова:")


@router.message(SetupStates.entering_json_path)
async def process_json_path(message: Message, state: FSMContext):
    path = message.text.strip()
    if path.lower() in ("none", "-"):
        path = ""
    await state.update_data(json_path=path)
    await message.answer(
        "ШАГ 3: API-ключ (необязательно)\n"
        "Если нужен Bearer-токен, отправьте его сейчас (он будет зашифрован).\n"
        "Иначе отправьте 'none':"
    )
    await state.set_state(SetupStates.entering_api_key)


@router.message(SetupStates.entering_api_key)
async def process_api_key(message: Message, state: FSMContext):
    api_key = message.text.strip()
    if api_key.lower() in ("none", "skip", "no", "-"):
        api_key = None
    data = await state.get_data()
    await DatabaseManager.update_bank_config(
        data["bank_id"], data["api_url"], data.get("json_path", ""), api_key
    )
    if data.get("is_editing"):
        await DatabaseManager.update_user_field(message.from_user.id, "bank_id", data["bank_id"])
        await DatabaseManager.update_user_field(message.from_user.id, "last_notified_rate", None)
        await state.clear()
        await message.answer("Настройки API сохранены. Банк обновлён.")
        await _show_settings_menu(message, state)
        return
    await message.answer("Настройки API сохранены.")
    await _proceed_to_threshold(message, state)


async def _proceed_to_threshold(event, state: FSMContext):
    await _reply(
        event,
        "Введите целевой порог курса.\n"
        "Бот уведомит вас, когда курс опустится до этого значения или ниже.\n\n"
        "Пример: 12.50",
    )
    await state.set_state(SetupStates.entering_threshold)


@router.message(SetupStates.entering_threshold)
async def process_threshold(message: Message, state: FSMContext):
    threshold = _parse_positive_float(message.text or "")
    if threshold is None:
        await message.answer("Некорректное число. Введите положительное значение, например 12.50")
        return
    data = await state.get_data()
    if data.get("is_editing"):
        await DatabaseManager.update_user_field(message.from_user.id, "threshold", threshold)
        await DatabaseManager.update_user_field(message.from_user.id, "last_notified_rate", None)
        await state.clear()
        await message.answer(f"Порог обновлён на {threshold}.")
        await _show_settings_menu(message, state)
        return
    await state.update_data(threshold=threshold)
    await message.answer(
        f"Порог установлен: {threshold}.\n"
        "Введите шаг уведомлений: повторное сообщение придёт, когда курс изменится "
        "на эту величину или больше в любую сторону.\n\n"
        "Пример: 0.10"
    )
    await state.set_state(SetupStates.entering_step)


@router.message(SetupStates.entering_step)
async def process_step(message: Message, state: FSMContext):
    step = _parse_positive_float(message.text or "")
    if step is None:
        await message.answer("Некорректное число. Введите положительное значение, например 0.10")
        return
    data = await state.get_data()
    if data.get("is_editing"):
        await DatabaseManager.update_user_field(message.from_user.id, "step", step)
        await DatabaseManager.update_user_field(message.from_user.id, "last_notified_rate", None)
        await state.clear()
        await message.answer(f"Шаг обновлён на {step}.")
        await _show_settings_menu(message, state)
        return
    await state.update_data(step=step)
    await message.answer(
        f"Шаг установлен: {step}.\nВыберите интервал проверки курса:",
        reply_markup=get_interval_keyboard(),
    )
    await state.set_state(SetupStates.choosing_interval)


@router.callback_query(SetupStates.choosing_interval, F.data.startswith("interval_"))
async def process_interval(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    data = await state.get_data()
    interval_sec = int(callback.data.split("_", 1)[1])
    if data.get("is_editing"):
        await DatabaseManager.update_user_field(callback.from_user.id, "interval_sec", interval_sec)
        await state.clear()
        await callback.message.answer(f"Интервал обновлён: {interval_sec // 60} мин.")
        await _show_settings_menu(callback, state)
        return
    if (
        not data
        or "currency" not in data
        or "bank_id" not in data
        or "threshold" not in data
        or "step" not in data
    ):
        await callback.message.answer(
            "Данные настройки были потеряны (перезапуск или повторное нажатие). "
            "Отправьте /start заново."
        )
        await state.clear()
        return
    current_time = int(time.time())
    await DatabaseManager.save_user_settings(
        callback.from_user.id,
        data["currency"],
        data["bank_id"],
        data["threshold"],
        data["step"],
        interval_sec,
        current_time + interval_sec,
    )
    await callback.message.answer(
        "Настройки сохранены, мониторинг запущен!\n"
        "Изменить параметры можно через /settings, а проверить курс вручную — через /check.\n\n"
        "Выполняю первую проверку прямо сейчас..."
    )
    await state.clear()
    await _run_immediate_check(bot, callback.from_user.id)


async def background_scheduler(bot: Bot):
    while True:
        try:
            current_time = int(time.time())
            due_users = await DatabaseManager.get_due_users(current_time)
            for row in due_users:
                (
                    user_id,
                    currency,
                    bank_id,
                    threshold,
                    step,
                    last_notified_rate,
                    error_count,
                    interval_sec,
                ) = row
                next_check = current_time + interval_sec

                details = await DatabaseManager.get_bank_details(bank_id)
                if not details:
                    await DatabaseManager.update_user_field(user_id, "next_check", next_check)
                    continue

                api_url, json_path, api_key, bank_name, parser_type = details
                parser = ParserFactory.get_parser(
                    parser_type, api_url or "", json_path or "", api_key
                )
                if not parser:
                    await DatabaseManager.update_user_field(user_id, "next_check", next_check)
                    continue

                res: FetchResult = await parser.fetch_rate_detailed(currency)

                if res.rate is not None:
                    await DatabaseManager.update_user_field(user_id, "error_count", 0)
                    if last_notified_rate is None:
                        should_notify = res.rate <= threshold
                    else:
                        should_notify = abs(res.rate - last_notified_rate) >= step
                    if should_notify:
                        try:
                            await bot.send_message(
                                user_id, f"💱 {currency} в {bank_name} — {res.rate}"
                            )
                            await DatabaseManager.update_user_field(
                                user_id, "last_notified_rate", res.rate
                            )
                        except Exception as e:
                            logger.error(f"Failed to send notification to user {user_id}: {e}")
                elif res.captcha:
                    try:
                        await _send_captcha_to_user(
                            bot=bot,
                            user_id=user_id,
                            bank_id=bank_id,
                            bank_name=bank_name,
                            currency=currency,
                            threshold=threshold,
                            step=step,
                            challenge=res.captcha,
                            force=False,
                        )
                    except Exception as e:
                        logger.error(f"Failed to send captcha prompt to user {user_id}: {e}")
                else:
                    new_count = error_count + 1
                    if new_count >= MAX_FETCH_ERRORS:
                        new_count = 0
                        try:
                            await bot.send_message(
                                user_id,
                                f"Ошибка: не удалось получить курс {currency} из {bank_name} "
                                f"({res.error or 'проверьте настройки API'}).",
                            )
                        except Exception:
                            pass
                    await DatabaseManager.update_user_field(user_id, "error_count", new_count)

                await DatabaseManager.update_user_field(user_id, "next_check", next_check)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(60)