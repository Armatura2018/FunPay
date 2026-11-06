from __future__ import annotations

import logging
import sys
import time

from app.config import settings, validate_settings
from app.funpay_storage import FunPayStorage
from app.funpay_worker import FunPayOrderWorker

try:
    from FunPayAPI import Account
    from FunPayAPI.updater.runner import Runner
    from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
except ImportError as exc:
    raise SystemExit(
        "Не найдена библиотека FunPayAPI. Выполните: bash scripts/install_funpay_api.sh "
        "и проверьте, что папка FunPayAPI находится рядом с main_funpay.py."
    ) from exc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("main")

def main() -> None:
    validate_settings()

    storage = FunPayStorage(settings.database_path)
    account = Account(
        golden_key=settings.funpay_golden_key,
        user_agent=settings.funpay_user_agent,
        locale="ru",
    )

    logger.info("Подключаюсь к FunPay...")
    account.get()
    logger.info("Авторизация успешна. Аккаунт: %s (ID %s)", account.username, account.id)

    # Снимок продаж нужен для безопасной инициализации Runner и восстановления пропущенных заказов.
    _, sales, _, _ = account.get_sales(
        include_paid=True, include_closed=True, include_refunded=True
    )
    worker = FunPayOrderWorker(account, storage, dry_run=settings.funpay_dry_run)
    worker.bootstrap_sales(sales, settings.funpay_first_start_baseline)

    runner = Runner(account)
    # Не даём первому опросу принять все старые заказы за новые.
    runner.saved_orders = {str(order.id): order for order in sales}

    storage.reset_interrupted_prompts()
    worker.dispatch_all_queued()

    logger.info(
        "Мониторинг запущен. Интервал: %.1f сек. Режим: %s",
        settings.funpay_requests_delay,
        "DRY RUN (сообщения не отправляются)" if settings.funpay_dry_run else "LIVE",
    )

    last_account_refresh = time.time()
    while True:
        started = time.monotonic()
        try:
            updates = runner.get_updates()
            events = runner.parse_updates(updates.get("objects", []))

            for event in events:
                if isinstance(event, NewOrderEvent):
                    worker.register_paid_order(event.order)
                elif isinstance(event, NewMessageEvent):
                    worker.handle_message(event.message)

        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C.")
            break
        except Exception:
            logger.exception("Ошибка цикла FunPay; попробую снова.")

        # По документации библиотеки сессию аккаунта полезно обновлять каждые 40–60 минут.
        if time.time() - last_account_refresh >= 40 * 60:
            try:
                account.get(update_phpsessid=True)
                last_account_refresh = time.time()
                logger.info("Сессия FunPay обновлена.")
            except Exception:
                logger.exception("Не удалось обновить сессию FunPay.")

        elapsed = time.monotonic() - started
        time.sleep(max(1.0, settings.funpay_requests_delay - elapsed))

if __name__ == "__main__":
    main()
