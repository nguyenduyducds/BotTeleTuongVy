import os
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "bot_data.db")))


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # Bảng lưu danh sách nhóm (tối đa 3 nhóm đang kích hoạt cùng lúc hoặc tùy chỉnh)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    username TEXT,
                    active INTEGER DEFAULT 1,
                    last_post_id INTEGER DEFAULT NULL,
                    last_sent_at TEXT DEFAULT NULL,
                    added_at TEXT NOT NULL
                )
            """)

            # Bảng lưu bài viết quảng cáo (nhận qua Forward hoặc gửi trực tiếp)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    preview_text TEXT,
                    content_text TEXT DEFAULT '',
                    media_path TEXT DEFAULT '',
                    media_type TEXT DEFAULT '',
                    storage_chat_id INTEGER NOT NULL,
                    storage_message_id INTEGER NOT NULL,
                    forward_origin_title TEXT,
                    send_mode TEXT DEFAULT 'forward',
                    max_runs INTEGER DEFAULT 3,
                    interval_minutes INTEGER DEFAULT 30,
                    fixed_times TEXT DEFAULT '',
                    active INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            # Tự động nâng cấp DB cũ nếu thiếu cột content_text / media_path / media_type
            existing_cols = {row["name"] for row in cursor.execute("PRAGMA table_info(posts)").fetchall()}
            for col_name in ("content_text", "media_path", "media_type"):
                if col_name not in existing_cols:
                    cursor.execute(f"ALTER TABLE posts ADD COLUMN {col_name} TEXT DEFAULT ''")

            # Bảng theo dõi số lần đã đăng của từng bài vào từng nhóm
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS post_group_progress (
                    post_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    runs_done INTEGER DEFAULT 0,
                    last_sent_at TEXT DEFAULT NULL,
                    last_message_id INTEGER DEFAULT NULL,
                    next_available_at TEXT DEFAULT NULL,
                    PRIMARY KEY (post_id, group_id),
                    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                    FOREIGN KEY (group_id) REFERENCES groups(chat_id) ON DELETE CASCADE
                )
            """)

            # Bảng cấu hình chung của hệ thống
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            # Bảng lịch sử gửi bài
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS send_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    group_title TEXT,
                    post_title TEXT,
                    status TEXT NOT NULL,
                    detail TEXT,
                    sent_at TEXT NOT NULL
                )
            """)

            default_settings = {
                "max_groups": "3",
                "stagger_minutes": "5",        # Độ lệch phút giữa các nhóm nếu chỉ có 1 bài
                "min_group_cooldown": "5",     # Thời gian nghỉ tối thiểu (phút) giữa 2 bài bất kỳ trong cùng 1 nhóm
                "delete_previous_ad": "0",     # 1 = Tự xoá bài cũ trong nhóm trước khi gửi bài mới
                "notify_on_complete": "1",     # 1 = Báo cho admin khi bài đăng hoàn thành đủ số lần
                "admin_chat_id": "",           # Chat ID của chủ bot để nhận thông báo
            }
            for k, v in default_settings.items():
                cursor.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, v),
                )
            conn.commit()

    # ================= SETTINGS =================
    def get_setting(self, key: str, default: str = "") -> str:
        with self._get_conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            conn.commit()

    # ================= GROUPS =================
    def add_or_update_group(self, chat_id: int, title: str, username: Optional[str] = None) -> Dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        with self._get_conn() as conn:
            existing = conn.execute("SELECT * FROM groups WHERE chat_id = ?", (chat_id,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE groups SET title = ?, username = ? WHERE chat_id = ?",
                    (title, username, chat_id),
                )
                conn.commit()
                return {"status": "updated", "active": bool(existing["active"])}

            active_count = conn.execute("SELECT COUNT(*) as cnt FROM groups WHERE active = 1").fetchone()["cnt"]
            max_groups = int(self.get_setting("max_groups", "3"))
            active = 1 if active_count < max_groups else 0

            conn.execute(
                """
                INSERT INTO groups (chat_id, title, username, active, added_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, title, username, active, now),
            )
            conn.commit()
            return {"status": "added", "active": bool(active), "active_count": active_count + (1 if active else 0)}

    def get_groups(self, active_only: bool = False) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            query = "SELECT * FROM groups"
            if active_only:
                query += " WHERE active = 1"
            query += " ORDER BY added_at ASC"
            rows = conn.execute(query).fetchall()
            return [dict(r) for r in rows]

    def get_group(self, chat_id: int) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM groups WHERE chat_id = ?", (chat_id,)).fetchone()
            return dict(row) if row else None

    def toggle_group(self, chat_id: int) -> Optional[bool]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT active FROM groups WHERE chat_id = ?", (chat_id,)).fetchone()
            if not row:
                return None
            new_state = 0 if row["active"] else 1
            if new_state == 1:
                max_groups = int(self.get_setting("max_groups", "3"))
                active_count = conn.execute("SELECT COUNT(*) as cnt FROM groups WHERE active = 1").fetchone()["cnt"]
                if active_count >= max_groups:
                    return False  # Đã đạt giới hạn 3 nhóm
            conn.execute("UPDATE groups SET active = ? WHERE chat_id = ?", (new_state, chat_id))
            conn.commit()
            return bool(new_state)

    def remove_group(self, chat_id: int):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM post_group_progress WHERE group_id = ?", (chat_id,))
            conn.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
            conn.commit()

    def update_group_last_post(self, chat_id: int, post_id: int, sent_at: str):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE groups SET last_post_id = ?, last_sent_at = ? WHERE chat_id = ?",
                (post_id, sent_at, chat_id),
            )
            conn.commit()

    # ================= POSTS =================
    def add_post(
        self,
        title: str,
        preview_text: str,
        storage_chat_id: int,
        storage_message_id: int,
        forward_origin_title: str = "",
        send_mode: str = "forward",
        max_runs: int = 3,
        interval_minutes: int = 30,
        content_text: str = "",
        media_path: str = "",
        media_type: str = "",
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO posts (
                    title, preview_text, content_text, media_path, media_type,
                    storage_chat_id, storage_message_id,
                    forward_origin_title, send_mode, max_runs, interval_minutes,
                    fixed_times, active, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?)
                """,
                (
                    title,
                    preview_text,
                    content_text,
                    media_path,
                    media_type,
                    storage_chat_id,
                    storage_message_id,
                    forward_origin_title,
                    send_mode,
                    max_runs,
                    interval_minutes,
                    now,
                ),
            )
            post_id = cursor.lastrowid
            conn.commit()
            return post_id

    def get_posts(self, active_only: bool = False) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            query = "SELECT * FROM posts"
            if active_only:
                query += " WHERE active = 1"
            query += " ORDER BY id ASC"
            rows = conn.execute(query).fetchall()
            return [dict(r) for r in rows]

    def get_post(self, post_id: int) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
            return dict(row) if row else None

    def update_post(self, post_id: int, **kwargs):
        if not kwargs:
            return
        fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [post_id]
        with self._get_conn() as conn:
            conn.execute(f"UPDATE posts SET {fields} WHERE id = ?", values)
            conn.commit()

    def delete_post(self, post_id: int):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM post_group_progress WHERE post_id = ?", (post_id,))
            conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            conn.commit()

    # ================= PROGRESS & ROTATION =================
    def ensure_progress_rows(self, post_id: int):
        groups = self.get_groups(active_only=True)
        with self._get_conn() as conn:
            for g in groups:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO post_group_progress (post_id, group_id, runs_done)
                    VALUES (?, ?, 0)
                    """,
                    (post_id, g["chat_id"]),
                )
            conn.commit()

    def reset_post_progress(self, post_id: int):
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE post_group_progress
                SET runs_done = 0, last_sent_at = NULL, next_available_at = NULL
                WHERE post_id = ?
                """,
                (post_id,),
            )
            conn.commit()
        self.ensure_progress_rows(post_id)

    def get_post_progress(self, post_id: int) -> List[Dict[str, Any]]:
        self.ensure_progress_rows(post_id)
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT p.*, g.title as group_title, g.active as group_active
                FROM post_group_progress p
                JOIN groups g ON g.chat_id = p.group_id
                WHERE p.post_id = ? AND g.active = 1
                ORDER BY g.added_at ASC
                """,
                (post_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_progress_entry(self, post_id: int, group_id: int) -> Dict[str, Any]:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO post_group_progress (post_id, group_id, runs_done)
                VALUES (?, ?, 0)
                """,
                (post_id, group_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM post_group_progress WHERE post_id = ? AND group_id = ?",
                (post_id, group_id),
            ).fetchone()
            return dict(row)

    def record_post_sent(
        self,
        post_id: int,
        group_id: int,
        sent_message_id: int,
        sent_at: str,
        next_available_at: str,
    ):
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO post_group_progress (post_id, group_id, runs_done, last_sent_at, last_message_id, next_available_at)
                VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(post_id, group_id) DO UPDATE SET
                    runs_done = runs_done + 1,
                    last_sent_at = excluded.last_sent_at,
                    last_message_id = excluded.last_message_id,
                    next_available_at = excluded.next_available_at
                """,
                (post_id, group_id, sent_at, sent_message_id, next_available_at),
            )
            conn.execute(
                "UPDATE groups SET last_post_id = ?, last_sent_at = ? WHERE chat_id = ?",
                (post_id, sent_at, group_id),
            )
            conn.commit()

    def add_log(self, post_id: int, group_id: int, group_title: str, post_title: str, status: str, detail: str = ""):
        now = datetime.now().isoformat(timespec="seconds")
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO send_logs (post_id, group_id, group_title, post_title, status, detail, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (post_id, group_id, group_title, post_title, status, detail, now),
            )
            # Giữ tối đa 200 log gần nhất
            conn.execute(
                """
                DELETE FROM send_logs WHERE id NOT IN (
                    SELECT id FROM send_logs ORDER BY id DESC LIMIT 200
                )
                """
            )
            conn.commit()

    def get_recent_logs(self, limit: int = 15) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM send_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
