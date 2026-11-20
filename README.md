# Currency Bot

Telegram-бот для отслеживания курсов валют (Сбербанк, Альфа-Банк, Т-Банк, АТБ, Инго Банк, Мосбиржа, ЦБ РФ и пользовательские API) с делегированием капчи пользователю через Telegram.

## Установка и запуск

1. Создайте и активируйте виртуальное окружение:
   ```bash
   python -m venv venv
   # Windows (CMD):
   venv\Scripts\activate.bat
   # Linux / macOS:
   source venv/bin/activate
   ```

2. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```

3. Скопируйте `.env.example` в `.env` и укажите свой `BOT_TOKEN`:
   ```bash
   copy .env.example .env
   ```

4. Запустите бота:
   ```bash
   python main.py
   ```

---
