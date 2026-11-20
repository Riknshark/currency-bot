import asyncio
from cryptography.fernet import Fernet, InvalidToken
from config import ENCRYPTION_KEY


class CryptoManager:
    """
    Handles symmetric encryption for sensitive data (API keys).
    Uses Fernet (AES-128-CBC) for encryption at rest.
    """
    _fernet = Fernet(ENCRYPTION_KEY.encode("utf-8"))

    @classmethod
    async def encrypt(cls, plaintext: str) -> str:
        token: bytes = await asyncio.to_thread(
            cls._fernet.encrypt, plaintext.encode("utf-8")
        )
        return token.decode("utf-8")

    @classmethod
    async def decrypt(cls, ciphertext: str | bytes) -> str | None:
        try:
            raw = ciphertext.encode("utf-8") if isinstance(ciphertext, str) else ciphertext
            decrypted = await asyncio.to_thread(cls._fernet.decrypt, raw)
            return decrypted.decode("utf-8")
        except (InvalidToken, Exception):
            return None