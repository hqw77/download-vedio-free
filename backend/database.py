"""SQLite 数据层: 建表, 用户, 配额, 订单."""

import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "app.db")  # 数据库文件


def get_db_path():
    """确保数据目录存在, 返回数据库路径."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return DB_PATH


@contextmanager
def get_db():
    """数据库连接上下文: 提交, 回滚, 关闭."""
    conn = sqlite3.connect(get_db_path())
    # 按列名取值
    conn.row_factory = sqlite3.Row
    # WAL 模式
    conn.execute("PRAGMA journal_mode=WAL")
    # 开启外键
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """初始化表结构."""
    # 建表: 用户, 订单, 索引
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_vip INTEGER DEFAULT 0,
                vip_expire_at TEXT,
                daily_summary_count INTEGER DEFAULT 0,
                last_summary_date TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT DEFAULT 'cny',
                status TEXT DEFAULT 'pending',
                plan_type TEXT DEFAULT 'monthly',
                stripe_session_id TEXT UNIQUE,
                stripe_payment_intent_id TEXT,
                paid_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_order_no ON orders(order_no);
            CREATE INDEX IF NOT EXISTS idx_orders_stripe_session_id ON orders(stripe_session_id);
        """)


FREE_DAILY_SUMMARY_LIMIT = 3  # 免费用户每日总结次数


def get_user_by_email(email: str) -> dict | None:
    """按邮箱查用户."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    """按 ID 查用户."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def create_user(email: str, password_hash: str) -> dict:
    """创建用户, 返回 ID 和邮箱."""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email, password_hash),
        )
        return {"id": cursor.lastrowid, "email": email}


def check_and_increment_summary(user_id: int) -> tuple[bool, int]:
    """检查并增加当日总结次数, 返回 (是否允许, 剩余次数)."""
    # UTC 日期
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        # 用户不存在
        if not user:
            return False, 0

        # VIP 未过期
        if user["is_vip"] and user["vip_expire_at"]:
            try:
                expire = datetime.fromisoformat(user["vip_expire_at"])
            except ValueError:
                expire = None
            if expire and expire > datetime.now(timezone.utc):
                return True, -1  # -1 means unlimited

        # 原子占用: 跨天重置为 1, 否则加 1; 只有未超限才更新
        cursor = conn.execute(
            """
            UPDATE users
            SET daily_summary_count = CASE
                    WHEN COALESCE(last_summary_date, '') = ? THEN COALESCE(daily_summary_count, 0) + 1
                    ELSE 1
                END,
                last_summary_date = ?
            WHERE id = ?
              AND (
                    COALESCE(last_summary_date, '') <> ?
                    OR COALESCE(daily_summary_count, 0) < ?
              )
            """,
            (today, today, user_id, today, FREE_DAILY_SUMMARY_LIMIT),
        )

        # 没有更新到, 说明次数已用完
        if cursor.rowcount == 0:
            return False, 0

        row = conn.execute(
            "SELECT daily_summary_count FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        count = row["daily_summary_count"] if row else 1
        return True, max(FREE_DAILY_SUMMARY_LIMIT - count, 0)


def refund_summary(user_id: int) -> None:
    """失败时退回一次当日配额."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET daily_summary_count = daily_summary_count - 1 "
            "WHERE id = ? AND last_summary_date = ? AND daily_summary_count > 0",
            (user_id, today),
        )


def create_order(user_id: int, order_no: str, amount: int, currency: str = "cny", plan_type: str = "monthly") -> dict:
    """创建待支付订单."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO orders (order_no, user_id, amount, currency, plan_type) VALUES (?, ?, ?, ?, ?)",
            (order_no, user_id, amount, currency, plan_type),
        )
        return {"order_no": order_no, "user_id": user_id, "amount": amount}


def update_order_stripe_session(order_no: str, session_id: str):
    """保存 Stripe Session ID."""
    with get_db() as conn:
        conn.execute(
            "UPDATE orders SET stripe_session_id = ?, updated_at = datetime('now') WHERE order_no = ?",
            (session_id, order_no),
        )


def complete_order(session_id: str, payment_intent_id: str) -> dict | None:
    """支付成功后更新订单和 VIP, 返回订单信息."""
    with get_db() as conn:
        # 只处理 pending 订单
        order = conn.execute(
            "SELECT * FROM orders WHERE stripe_session_id = ? AND status = 'pending'",
            (session_id,),
        ).fetchone()

        if not order:
            return None

        now = datetime.now(timezone.utc).isoformat()

        from dateutil.relativedelta import relativedelta
        # 读取用户
        user = conn.execute("SELECT * FROM users WHERE id = ?", (order["user_id"],)).fetchone()

        current_expire = None
        if user["vip_expire_at"]:
            try:
                current_expire = datetime.fromisoformat(user["vip_expire_at"])
            except ValueError:
                pass

        # 计算新的到期时间
        base_time = datetime.now(timezone.utc)
        if current_expire and current_expire > base_time:
            base_time = current_expire

        if order["plan_type"] == "monthly":
            new_expire = base_time + relativedelta(months=1)
        elif order["plan_type"] == "yearly":
            new_expire = base_time + relativedelta(years=1)
        else:
            new_expire = base_time + relativedelta(months=1)

        # 更新订单
        conn.execute(
            "UPDATE orders SET status = 'paid', stripe_payment_intent_id = ?, paid_at = ?, updated_at = ? WHERE id = ?",
            (payment_intent_id, now, now, order["id"]),
        )

        # 更新用户
        conn.execute(
            "UPDATE users SET is_vip = 1, vip_expire_at = ?, updated_at = ? WHERE id = ?",
            (new_expire.isoformat(), now, order["user_id"]),
        )

        return dict(order)


def get_order_by_no(order_no: str) -> dict | None:
    """按订单号查订单."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        return dict(row) if row else None


def get_user_orders(user_id: int) -> list[dict]:
    """查用户订单列表."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
