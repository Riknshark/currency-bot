import asyncio
import json
import sqlite3
import time
from typing import Optional

from config import DB_PATH
from services.crypto import CryptoManager

ALLOWED_USER_FIELDS = {
    "currency",
    "bank_id",
    "threshold",
    "step",
    "interval_sec",
    "next_check",
    "last_notified_rate",
    "error_count",
}

BUILTIN_BANKS = [
    ("sber", "Сбербанк (Sberbank)", "sber", 1),
    ("alfa", "Альфа-Банк (Alfa-Bank)", "alfa", 1),
    ("tinkoff", "Т-Банк (Tinkoff)", "tbank", 1),
    ("atb", "АТБ (Азиатско-Тихоокеанский Банк)", "atb", 1),
    ("ingo", "Инго Банк (Ingo)", "ingo_page", 1),
    ("moex", "Мосбиржа (MOEX)", "moex", 1),
    ("cbrf", "ЦБ РФ (CBRF)", "cbrf", 1),
]


class DatabaseManager:
    @staticmethod
    def _connect():
        return sqlite3.connect(DB_PATH, timeout=10)

    @staticmethod
    async def init_db():
        def _init():
            with DatabaseManager._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS banks (
                        bank_id TEXT PRIMARY KEY,
                        display_name TEXT,
                        api_url TEXT,
                        json_path TEXT,
                        api_key_encrypted TEXT,
                        parser_type TEXT DEFAULT 'universal_json',
                        is_configured BOOLEAN DEFAULT 0
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        currency TEXT,
                        bank_id TEXT,
                        threshold REAL,
                        step REAL,
                        interval_sec INTEGER,
                        next_check INTEGER,
                        last_notified_rate REAL,
                        error_count INTEGER DEFAULT 0
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS domain_cookies (
                        domain TEXT PRIMARY KEY,
                        cookies_json TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    )
                """)

                cursor = conn.execute("PRAGMA table_info(banks)")
                columns = [row[1] for row in cursor.fetchall()]
                if "parser_type" not in columns:
                    conn.execute(
                        "ALTER TABLE banks ADD COLUMN parser_type TEXT DEFAULT 'universal_json'"
                    )

                for bank_id, display_name, parser_type, is_configured in BUILTIN_BANKS:
                    row = conn.execute(
                        "SELECT display_name, api_url, parser_type, is_configured "
                        "FROM banks WHERE bank_id=?",
                        (bank_id,),
                    ).fetchone()
                    if row is None:
                        conn.execute(
                            "INSERT INTO banks (bank_id, display_name, parser_type, is_configured) "
                            "VALUES (?, ?, ?, ?)",
                            (bank_id, display_name, parser_type, is_configured),
                        )
                    else:
                        old_name, old_url, old_parser, old_conf = row
                        needs_upgrade = (
                            not old_conf
                            or (not old_url and old_parser in ("universal_json", "ingobank", "ingo_page"))
                            or (bank_id == "tinkoff" and old_parser != "tbank")
                        )
                        if needs_upgrade:
                            conn.execute(
                                "UPDATE banks SET display_name=?, parser_type=?, is_configured=? "
                                "WHERE bank_id=?",
                                (display_name, parser_type, is_configured, bank_id),
                            )
                        elif bank_id == "atb" and old_name == "ATB (Atlantico)":
                            conn.execute(
                                "UPDATE banks SET display_name=? WHERE bank_id=?",
                                (display_name, bank_id),
                            )

                conn.commit()

        await asyncio.to_thread(_init)

    @staticmethod
    async def get_all_banks():
        def _fetch():
            with DatabaseManager._connect() as conn:
                cursor = conn.execute(
                    "SELECT bank_id, display_name, is_configured FROM banks ORDER BY rowid"
                )
                return cursor.fetchall()

        return await asyncio.to_thread(_fetch)

    @staticmethod
    async def get_bank_details(bank_id: str):
        def _fetch():
            with DatabaseManager._connect() as conn:
                cursor = conn.execute(
                    "SELECT api_url, json_path, api_key_encrypted, display_name, parser_type "
                    "FROM banks WHERE bank_id=?",
                    (bank_id,),
                )
                return cursor.fetchone()

        row = await asyncio.to_thread(_fetch)
        if not row:
            return None
        api_url, json_path, encrypted_key, display_name, parser_type = row
        decrypted_key = await CryptoManager.decrypt(encrypted_key) if encrypted_key else None
        return api_url, json_path, decrypted_key, display_name, parser_type

    @staticmethod
    async def update_bank_config(bank_id: str, api_url: str, json_path: str, api_key: str | None):
        encrypted_key = await CryptoManager.encrypt(api_key) if api_key else None

        def _update():
            with DatabaseManager._connect() as conn:
                conn.execute(
                    "UPDATE banks SET api_url=?, json_path=?, api_key_encrypted=?, is_configured=1 "
                    "WHERE bank_id=?",
                    (api_url, json_path, encrypted_key, bank_id),
                )
                conn.commit()

        await asyncio.to_thread(_update)

    @staticmethod
    async def add_custom_bank(bank_id: str, display_name: str):
        def _add():
            with DatabaseManager._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO banks (bank_id, display_name, parser_type, is_configured) "
                    "VALUES (?, ?, 'universal_json', 0)",
                    (bank_id, display_name),
                )
                conn.commit()

        await asyncio.to_thread(_add)

    @staticmethod
    async def save_user_settings(
        user_id: int,
        currency: str,
        bank_id: str,
        threshold: float,
        step: float,
        interval_sec: int,
        next_check: int,
    ):
        def _save():
            with DatabaseManager._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO users "
                    "(user_id, currency, bank_id, threshold, step, interval_sec, next_check, "
                    "last_notified_rate, error_count) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                    (user_id, currency, bank_id, threshold, step, interval_sec, next_check),
                )
                conn.commit()

        await asyncio.to_thread(_save)

    @staticmethod
    async def update_user_field(user_id: int, field: str, value):
        if field not in ALLOWED_USER_FIELDS:
            raise ValueError(f"Disallowed user field: {field}")

        def _update():
            with DatabaseManager._connect() as conn:
                conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))
                conn.commit()

        await asyncio.to_thread(_update)

    @staticmethod
    async def get_user_settings(user_id: int):
        def _fetch():
            with DatabaseManager._connect() as conn:
                cursor = conn.execute(
                    "SELECT currency, bank_id, threshold, step, interval_sec "
                    "FROM users WHERE user_id=?",
                    (user_id,),
                )
                return cursor.fetchone()

        return await asyncio.to_thread(_fetch)

    @staticmethod
    async def get_user_full(user_id: int):
        def _fetch():
            with DatabaseManager._connect() as conn:
                cursor = conn.execute(
                    "SELECT user_id, currency, bank_id, threshold, step, "
                    "last_notified_rate, error_count, interval_sec, next_check "
                    "FROM users WHERE user_id=?",
                    (user_id,),
                )
                return cursor.fetchone()

        return await asyncio.to_thread(_fetch)

    @staticmethod
    async def get_due_users(current_time: int):
        def _fetch():
            with DatabaseManager._connect() as conn:
                cursor = conn.execute(
                    "SELECT user_id, currency, bank_id, threshold, step, last_notified_rate, "
                    "error_count, interval_sec FROM users WHERE next_check <= ?",
                    (current_time,),
                )
                return cursor.fetchall()

        return await asyncio.to_thread(_fetch)

    @staticmethod
    async def get_all_domain_cookies() -> dict[str, dict[str, str]]:
        def _fetch():
            with DatabaseManager._connect() as conn:
                rows = conn.execute(
                    "SELECT domain, cookies_json FROM domain_cookies"
                ).fetchall()
            result = {}
            for domain, raw in rows:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        result[domain] = {str(k): str(v) for k, v in data.items()}
                except Exception:
                    continue
            return result

        return await asyncio.to_thread(_fetch)

    @staticmethod
    async def save_domain_cookies(domain: str, cookies: dict[str, str]):
        payload = json.dumps(cookies, ensure_ascii=False)
        now = int(time.time())

        def _save():
            with DatabaseManager._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO domain_cookies (domain, cookies_json, updated_at) "
                    "VALUES (?, ?, ?)",
                    (domain, payload, now),
                )
                conn.commit()

        await asyncio.to_thread(_save)