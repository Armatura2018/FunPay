from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

@dataclass(frozen=True)
class Settings:
    database_path: str = os.getenv("DATABASE_PATH", "/app/data/bot.db")
    funpay_golden_key: str = os.getenv("FUNPAY_GOLDEN_KEY", "").strip()
    funpay_user_agent: str = os.getenv("FUNPAY_USER_AGENT", "").strip() or None
    funpay_requests_delay: float = float(os.getenv("FUNPAY_REQUESTS_DELAY", "8"))
    funpay_dry_run: bool = _bool("FUNPAY_DRY_RUN", True)
    funpay_first_start_baseline: bool = _bool("FUNPAY_FIRST_START_BASELINE", True)

settings = Settings()

def validate_settings() -> None:
    if not settings.funpay_golden_key:
        raise RuntimeError(
            "Не задан FUNPAY_GOLDEN_KEY. Добавьте его в переменные окружения хостинга "
            "или в локальный .env."
        )
    if settings.funpay_requests_delay < 5:
        raise RuntimeError(
            "FUNPAY_REQUESTS_DELAY меньше 5 секунд. Установите 8–10 секунд, "
            "чтобы не создавать избыточную нагрузку на FunPay."
        )
