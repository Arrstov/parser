"""Конфигурация: берём значения из переменных окружения или файла .env."""
import os


def _load_dotenv(path: str = ".env") -> None:
    """Минимальный загрузчик .env (без внешних зависимостей)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION_NAME = os.getenv("TELEGRAM_SESSION", "chat_finder")

# Минимальное число подписчиков, чтобы чат/канал считался "популярным"
MIN_SUBSCRIBERS = int(os.getenv("MIN_SUBSCRIBERS", "500"))

# Сколько результатов искать по каждому ключевому слову
RESULTS_PER_KEYWORD = int(os.getenv("RESULTS_PER_KEYWORD", "50"))

# Прокси для подключения к Telegram (нужен, если сеть блокирует DC).
# Формат: socks5://127.0.0.1:1080  или  http://127.0.0.1:8888
# (для PySocks-прокси в telethon это тот же формат, что и в tdl/mtproxy)
PROXY_URL = os.getenv("PROXY_URL", "").strip()
