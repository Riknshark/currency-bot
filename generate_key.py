import os
from cryptography.fernet import Fernet


def generate_and_save_key() -> str:
    env_path = ".env"
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("BOT_TOKEN=your_telegram_bot_token_here\n")

    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ENCRYPTION_KEY="):
            existing = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if existing:
                print("Encryption key already exists in .env")
                return existing

    key = Fernet.generate_key().decode("utf-8")
    with open(env_path, "a", encoding="utf-8") as f:
        if lines and not lines[-1].endswith("\n"):
            f.write("\n")
        f.write(f"ENCRYPTION_KEY={key}\n")
    print("Encryption key generated and saved to .env")
    return key


if __name__ == "__main__":
    generate_and_save_key()