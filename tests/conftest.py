"""Тесты не должны зависеть от локального .env разработчика.

Пустые значения в os.environ имеют приоритет над .env (load_dotenv(override=False)),
поэтому тесты всегда стартуют в одном и том же «чистом» режиме.
"""
import os

for _name in ("API_SECRET_KEY", "APP_SECRET_KEY", "SECRET_KEY", "TELEGRAM_BOT_TOKEN",
              "TELEGRAM_ADMIN_IDS", "TELEGRAM_ALLOWED_USER_IDS"):
    os.environ[_name] = ""
os.environ["APP_ENV"] = "development"
os.environ["AUTO_START_BOT"] = "0"
os.environ["API_ALLOW_INSECURE_DEMO"] = "1"
