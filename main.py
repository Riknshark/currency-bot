import asyncio
import logging
import socket

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import BOT_TOKEN, PROXY_URL
from database.manager import DatabaseManager
from services.captcha_bridge import CookieStore
from bot.handlers import router, background_scheduler


async def set_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Начать настройку"),
        BotCommand(command="check", description="Проверить курс сейчас"),
        BotCommand(command="settings", description="Изменить параметры"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logging.warning(f"Failed to set bot commands: {e}")


def create_bot() -> Bot:
    if PROXY_URL:
        logging.info(f"Using proxy for Telegram API: {PROXY_URL}")
        session = AiohttpSession(proxy=PROXY_URL)  #
    else:
        session = AiohttpSession()
        # Force IPv4 to avoid Windows IPv6 semaphore timeouts
        session._connector_init["family"] = socket.AF_INET  #
    return Bot(token=BOT_TOKEN, session=session)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not BOT_TOKEN or BOT_TOKEN == "your_telegram_bot_token_here":
        raise ValueError("BOT_TOKEN is not set in .env or environment variables.")

    await DatabaseManager.init_db()
    await CookieStore.load_from_db()

    bot = create_bot()
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler_task = asyncio.create_task(background_scheduler(bot))

    try:
        while True:
            try:
                await set_commands(bot)
                await dp.start_polling(bot, drop_pending_updates=True)
                break
            except TelegramNetworkError as e:
                logging.error(
                    f"Нет подключения к api.telegram.org ({e}). "
                    "Включите VPN (TUN) или задайте PROXY_URL в .env. Повтор через 10 сек..."
                )
                await asyncio.sleep(10)
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен пользователем")