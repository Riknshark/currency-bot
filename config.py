import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from generate_key import generate_and_save_key

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
PROXY_URL = os.getenv("PROXY_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")

if not ENCRYPTION_KEY:
    try:
        ENCRYPTION_KEY = generate_and_save_key()
        load_dotenv(override=True)
    except Exception:
        ENCRYPTION_KEY = Fernet.generate_key().decode("utf-8")

MIN_REQUEST_INTERVAL = 900  # 15 minutes minimum
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
MAX_FETCH_ERRORS = 3