from __future__ import annotations

from pathlib import Path
import sqlite3
from contextlib import contextmanager
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS funpay_orders (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funpay_order_id TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    buyer_id INTEGER NOT NULL,
    buyer_username TEXT,
    subcategory_name TEXT,
    lot_title TEXT NOT NULL,
    lot_params_json TEXT NOT NULL DEFAULT '{}',
    quantity INTEGER,
    link TEXT,
    status TEXT NOT NULL,
    prompt_message_id INTEGER,
    response_message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    link_received_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_funpay_orders_buyer_status
ON funpay_orders(buyer_id, status, row_id);

CREATE TABLE IF NOT EXISTS funpay_seen_orders (
    funpay_order_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    funpay_order_id TEXT,
    buyer_id INTEGER,
    event_type TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

class FunPayStorage:
    def __init__(self, database_path: str):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_meta(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO bot_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def is_seen(self, order_id: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM funpay_seen_orders WHERE funpay_order_id=?", (str(order_id),)
            ).fetchone() is not None

    def mark_seen(self, order_id: str, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO funpay_seen_orders(funpay_order_id,reason) VALUES(?,?)",
                (str(order_id), reason),
            )

    def enqueue_order(self, data: dict[str, Any]) -> bool:
        """Добавляет заказ в очередь и помечает ID увиденным в одной транзакции."""
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM funpay_seen_orders WHERE funpay_order_id=?",
                (str(data["funpay_order_id"]),),
            ).fetchone()
            if exists:
                return False
            conn.execute(
                """INSERT INTO funpay_orders(
                    funpay_order_id,chat_id,buyer_id,buyer_username,
                    subcategory_name,lot_title,lot_params_json,quantity,status
                ) VALUES(?,?,?,?,?,?,?,?, 'queued_for_link')""",
                (
                    str(data["funpay_order_id"]), str(data["chat_id"]), int(data["buyer_id"]),
                    data.get("buyer_username"), data.get("subcategory_name"),
                    data["lot_title"], data.get("lot_params_json", "{}"),
                    data.get("quantity"),
                ),
            )
            conn.execute(
                "INSERT INTO funpay_seen_orders(funpay_order_id,reason) VALUES(?,?)",
                (str(data["funpay_order_id"]), "queued_for_link"),
            )
            conn.execute(
                "INSERT INTO bot_events(funpay_order_id,buyer_id,event_type,details) "
                "VALUES(?,?,?,?)",
                (str(data["funpay_order_id"]), int(data["buyer_id"]),
                 "new_paid_order", data["lot_title"]),
            )
            return True

    def get_active_order_for_buyer(self, buyer_id: int) -> dict | None:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM funpay_orders
                   WHERE buyer_id=? AND status='awaiting_link'
                   ORDER BY row_id LIMIT 2""",
                (int(buyer_id),),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError(
                    f"У покупателя {buyer_id} найдено больше одного активного запроса ссылки."
                )
            return dict(rows[0]) if rows else None

    def reserve_next_queued_order(self, buyer_id: int) -> dict | None:
        with self.connect() as conn:
            active = conn.execute(
                "SELECT 1 FROM funpay_orders WHERE buyer_id=? AND status='awaiting_link' LIMIT 1",
                (int(buyer_id),),
            ).fetchone()
            if active:
                return None
            row = conn.execute(
                """SELECT * FROM funpay_orders
                   WHERE buyer_id=? AND status='queued_for_link'
                   ORDER BY row_id LIMIT 1""",
                (int(buyer_id),),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE funpay_orders SET status='sending_prompt',updated_at=CURRENT_TIMESTAMP "
                "WHERE funpay_order_id=? AND status='queued_for_link'",
                (row["funpay_order_id"],),
            )
            return dict(row)

    def set_prompt_sent(self, order_id: str, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE funpay_orders
                   SET status='awaiting_link',prompt_message_id=?,updated_at=CURRENT_TIMESTAMP
                   WHERE funpay_order_id=? AND status='sending_prompt'""",
                (int(message_id), str(order_id)),
            )
            conn.execute(
                "INSERT INTO bot_events(funpay_order_id,event_type,details) VALUES(?,?,?)",
                (str(order_id), "link_requested", f"prompt_message_id={message_id}"),
            )

    def set_prompt_failed(self, order_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE funpay_orders SET status='queued_for_link',updated_at=CURRENT_TIMESTAMP "
                "WHERE funpay_order_id=? AND status='sending_prompt'",
                (str(order_id),),
            )
            conn.execute(
                "INSERT INTO bot_events(funpay_order_id,event_type,details) VALUES(?,?,?)",
                (str(order_id), "prompt_send_failed", error[:1000]),
            )

    def save_link(self, order_id: str, link: str, message_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE funpay_orders
                   SET link=?,status='link_received',response_message_id=?,
                       link_received_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                   WHERE funpay_order_id=? AND status='awaiting_link'""",
                (link, int(message_id), str(order_id)),
            )
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO bot_events(funpay_order_id,event_type,details) VALUES(?,?,?)",
                    (str(order_id), "link_received", link),
                )
            return cur.rowcount == 1

    def queued_buyer_ids(self) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT buyer_id FROM funpay_orders WHERE status='queued_for_link'"
            ).fetchall()
            return [int(row["buyer_id"]) for row in rows]

    def reset_interrupted_prompts(self) -> None:
        # После аварийного завершения повторная отправка возможна.
        # При редком сбое между отправкой сообщения и записью его ID возможен дубль.
        with self.connect() as conn:
            conn.execute(
                "UPDATE funpay_orders SET status='queued_for_link',updated_at=CURRENT_TIMESTAMP "
                "WHERE status='sending_prompt'"
            )
