from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit
from typing import Any

from app.funpay_storage import FunPayStorage

logger = logging.getLogger("funpay_worker")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

def extract_single_url(text: str | None) -> str | None:
    """Принимает ровно одну корректную HTTP(S)-ссылку из сообщения."""
    if not text:
        return None
    matches = URL_RE.findall(text.strip())
    if len(matches) != 1:
        return None
    url = matches[0].rstrip(".,!?;:)]}")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if "." not in parsed.hostname:
            return None
        return url
    except ValueError:
        return None

def _status_name(status: Any) -> str:
    return getattr(status, "name", str(status)).upper().rsplit(".", 1)[-1]

def _quantity_from_order(full_order: Any, shortcut: Any) -> int | None:
    # Сначала встроенное количество из полной карточки заказа.
    amount = getattr(full_order, "amount", None) if full_order is not None else None
    try:
        if amount is not None and int(float(amount)) > 0:
            return int(float(amount))
    except (TypeError, ValueError):
        pass

    # Затем пользовательские поля лота, например «Количество».
    params = getattr(full_order, "lot_params_dict", {}) if full_order is not None else {}
    if isinstance(params, dict):
        for name, value in params.items():
            normalized = str(name).strip().lower()
            if any(word in normalized for word in ("колич", "quantity", "amount", "объем", "объём")):
                match = re.search(r"\d[\d\s]*", str(value))
                if match:
                    digits = re.sub(r"\s+", "", match.group(0))
                    if digits.isdigit() and int(digits) > 0:
                        return int(digits)

    # Последний вариант — количество, распознанное из описания заказа.
    guessed = getattr(shortcut, "amount", None)
    try:
        if guessed is not None and int(guessed) > 1:
            return int(guessed)
    except (TypeError, ValueError):
        pass
    return None

class FunPayOrderWorker:
    def __init__(self, account: Any, storage: FunPayStorage, dry_run: bool = True):
        self.account = account
        self.storage = storage
        self.dry_run = dry_run

    def bootstrap_sales(self, sales: list[Any], first_start_baseline: bool) -> None:
        baseline_done = self.storage.get_meta("funpay_initial_baseline_done") == "1"

        if not baseline_done and first_start_baseline:
            # Не пишем старым покупателям при первой установке.
            for sale in sales:
                self.storage.mark_seen(str(sale.id), "first_start_baseline")
            self.storage.set_meta("funpay_initial_baseline_done", "1")
            logger.info(
                "Первый запуск: %s уже существующих продаж добавлены в базовый список без сообщений.",
                len(sales),
            )
            return

        # После перезапуска восстанавливаем пропущенные заказы, которые всё ещё оплачены.
        for sale in sales:
            order_id = str(sale.id)
            if self.storage.is_seen(order_id):
                continue
            if _status_name(getattr(sale, "status", "")) == "PAID":
                self.register_paid_order(sale)
            else:
                self.storage.mark_seen(order_id, "startup_non_paid")

        self.storage.set_meta("funpay_initial_baseline_done", "1")

    def register_paid_order(self, sale: Any) -> None:
        order_id = str(sale.id)
        if self.storage.is_seen(order_id):
            return

        if _status_name(getattr(sale, "status", "")) != "PAID":
            self.storage.mark_seen(order_id, "not_paid")
            return

        full_order = None
        try:
            full_order = self.account.get_order(order_id)
            if _status_name(getattr(full_order, "status", "")) != "PAID":
                self.storage.mark_seen(order_id, "full_order_not_paid")
                return
        except Exception:
            logger.exception("Не удалось получить детали FunPay-заказа #%s; сохраняю краткие данные.", order_id)

        title = (
            getattr(full_order, "short_description", None)
            if full_order is not None else None
        ) or getattr(sale, "description", None) or "Услуга из заказа FunPay"

        subcategory = getattr(sale, "subcategory_name", None) or ""
        if full_order is not None and getattr(full_order, "subcategory", None):
            subcategory = getattr(full_order.subcategory, "name", None) or subcategory

        params = getattr(full_order, "lot_params_dict", {}) if full_order is not None else {}
        if not isinstance(params, dict):
            params = {}

        quantity = _quantity_from_order(full_order, sale)
        chat_id = getattr(full_order, "chat_id", None) if full_order is not None else None
        chat_id = chat_id or getattr(sale, "chat_id", None)
        buyer_id = getattr(full_order, "buyer_id", None) if full_order is not None else None
        buyer_id = buyer_id or getattr(sale, "buyer_id", None)
        buyer_username = getattr(full_order, "buyer_username", None) if full_order is not None else None
        buyer_username = buyer_username or getattr(sale, "buyer_username", None)

        if not chat_id or not buyer_id:
            logger.error("У заказа #%s не удалось определить чат или ID покупателя.", order_id)
            return

        row = {
            "funpay_order_id": order_id,
            "chat_id": str(chat_id),
            "buyer_id": int(buyer_id),
            "buyer_username": buyer_username,
            "subcategory_name": subcategory,
            "lot_title": str(title),
            "lot_params_json": json.dumps(params, ensure_ascii=False, default=str),
            "quantity": quantity,
        }
        inserted = self.storage.enqueue_order(row)
        if inserted:
            logger.info(
                "Новый оплаченный заказ #%s: покупатель=%s, лот=%s, количество=%s",
                order_id, buyer_username or buyer_id, title, quantity,
            )
            self.dispatch_next_for_buyer(int(buyer_id))

    def dispatch_next_for_buyer(self, buyer_id: int) -> None:
        row = self.storage.reserve_next_queued_order(buyer_id)
        if not row:
            return

        quantity_line = f"\nКоличество: {row['quantity']}" if row.get("quantity") else ""
        message = (
            f"Здравствуйте! Спасибо за заказ #{row['funpay_order_id']}.\n"
            f"Услуга: {row['lot_title']}{quantity_line}\n\n"
            "Отправьте, пожалуйста, ссылку на публикацию одним сообщением."
        )

        if self.dry_run:
            logger.info(
                "[DRY RUN] Покупателю %s было бы отправлено сообщение по заказу #%s:\n%s",
                row["buyer_username"] or buyer_id, row["funpay_order_id"], message,
            )
            self.storage.set_prompt_failed(row["funpay_order_id"], "DRY_RUN: сообщение не отправлялось")
            return

        try:
            sent = self.account.send_message(
                chat_id=row["chat_id"],
                text=message,
                chat_name=row["buyer_username"],
                interlocutor_id=buyer_id,
            )
            self.storage.set_prompt_sent(row["funpay_order_id"], int(getattr(sent, "id", 0) or 0))
            logger.info("Запрос ссылки отправлен покупателю по заказу #%s.", row["funpay_order_id"])
        except Exception as exc:
            self.storage.set_prompt_failed(row["funpay_order_id"], repr(exc))
            logger.exception("Не удалось отправить запрос ссылки по заказу #%s.", row["funpay_order_id"])

    def dispatch_all_queued(self) -> None:
        for buyer_id in self.storage.queued_buyer_ids():
            self.dispatch_next_for_buyer(buyer_id)

    def handle_message(self, message: Any) -> None:
        author_id = getattr(message, "author_id", None)
        if author_id in (None, 0, self.account.id):
            return

        pending = self.storage.get_active_order_for_buyer(int(author_id))
        if not pending:
            return

        message_id = int(getattr(message, "id", 0) or 0)
        prompt_id = pending.get("prompt_message_id")
        if prompt_id is not None and message_id <= int(prompt_id):
            logger.debug("Игнорирую сообщение до запроса ссылки: buyer=%s, msg=%s", author_id, message_id)
            return

        url = extract_single_url(getattr(message, "text", None))
        if not url:
            if self.dry_run:
                logger.info("[DRY RUN] Покупателю %s нужно попросить прислать одну ссылку.", author_id)
            else:
                try:
                    self.account.send_message(
                        chat_id=message.chat_id,
                        text="Не получилось распознать ссылку. Пришлите, пожалуйста, одну полную ссылку на публикацию.",
                        chat_name=getattr(message, "chat_name", None),
                        interlocutor_id=author_id,
                    )
                except Exception:
                    logger.exception("Не удалось попросить покупателя прислать корректную ссылку.")
            return

        saved = self.storage.save_link(
            pending["funpay_order_id"], url, message_id
        )
        if not saved:
            return

        logger.info(
            "Ссылка сохранена: заказ #%s, покупатель=%s, ссылка=%s",
            pending["funpay_order_id"], author_id, url,
        )

        if not self.dry_run:
            try:
                self.account.send_message(
                    chat_id=message.chat_id,
                    text="Спасибо! Ссылку получил и сохранил для вашего заказа.",
                    chat_name=getattr(message, "chat_name", None),
                    interlocutor_id=author_id,
                )
            except Exception:
                logger.exception("Ссылка сохранена, но подтверждение покупателю отправить не удалось.")

        # Следующий заказ того же покупателя получит отдельный запрос ссылки.
        self.dispatch_next_for_buyer(int(author_id))
