"""
userbot.py — Module Telethon Userbot
Dùng tài khoản Telegram cá nhân (đăng nhập bằng SĐT + OTP) để:
  1. Dò danh sách tất cả nhóm đang tham gia → gửi về Bot để chọn.
  2. Gửi/Forward bài viết vào các nhóm đã chọn (thay thế hoàn toàn Bot Token,
     giải quyết vấn đề Bot không có trong nhóm).
  3. Chạy song song với python-telegram-bot qua asyncio.
"""

import asyncio
import logging
import os
import html
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    ChannelPrivateError,
    PeerFloodError,
)

load_dotenv()
logger = logging.getLogger(__name__)

SESSION_FILE = Path(__file__).parent / "userbot_session"


class UserbotManager:
    def __init__(self, db):
        self.db = db
        self.client: Optional[TelegramClient] = None
        self._scan_cache: list[dict] = []   # Cache danh sách nhóm quét được
        self._scan_at: Optional[datetime] = None
        self._pending_code_callback = None   # Callback nhập OTP qua Bot
        self._phone: str = ""
        self._phone_code_hash: str = ""
        self._notify_chat_id: Optional[int] = None   # Chat ID admin để gửi thông báo

    # ==================================================================
    # KHỞI TẠO CLIENT
    # ==================================================================
    def _build_client(self) -> TelegramClient:
        api_id = int(os.getenv("TG_API_ID", "0"))
        api_hash = os.getenv("TG_API_HASH", "")
        return TelegramClient(str(SESSION_FILE), api_id, api_hash)

    async def start_silent(self) -> bool:
        """Thử kết nối lại bằng session đã lưu (không yêu cầu OTP nếu session còn hạn)."""
        api_id = int(os.getenv("TG_API_ID", "0"))
        api_hash = os.getenv("TG_API_HASH", "")
        if not api_id or not api_hash:
            return False
        try:
            self.client = self._build_client()
            await self.client.connect()
            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                logger.info(f"[Userbot] Đã kết nối tài khoản: {me.first_name} (+{me.phone})")
                self.db.set_setting("userbot_name", f"{me.first_name or ''} {me.last_name or ''}".strip())
                self.db.set_setting("userbot_phone", f"+{me.phone}")
                self.db.set_setting("userbot_active", "1")
                return True
            return False
        except Exception as e:
            logger.warning(f"[Userbot] Không thể kết nối silent: {e}")
            return False

    async def request_code(self, phone: str) -> str:
        """Gửi yêu cầu mã OTP đến SĐT và trả về trạng thái."""
        api_id = int(os.getenv("TG_API_ID", "0"))
        api_hash = os.getenv("TG_API_HASH", "")
        if not api_id or not api_hash:
            return "NO_API_CREDS"
        self._phone = phone.strip()
        try:
            self.client = self._build_client()
            await self.client.connect()
            result = await self.client.send_code_request(self._phone)
            self._phone_code_hash = result.phone_code_hash
            return "CODE_SENT"
        except Exception as e:
            return f"ERROR:{e}"

    async def sign_in(self, code: str, password: str = "") -> str:
        """Đăng nhập bằng mã OTP (và mật khẩu 2FA nếu có)."""
        if not self.client:
            return "NO_CLIENT"
        try:
            await self.client.sign_in(
                phone=self._phone,
                code=code.strip(),
                phone_code_hash=self._phone_code_hash,
            )
        except Exception as two_fa_exc:
            if "SessionPasswordNeeded" in type(two_fa_exc).__name__:
                if not password:
                    return "NEED_2FA"
                try:
                    await self.client.sign_in(password=password)
                except Exception as e2:
                    return f"2FA_WRONG:{e2}"
            else:
                return f"ERROR:{two_fa_exc}"

        me = await self.client.get_me()
        self.db.set_setting("userbot_name", f"{me.first_name or ''} {me.last_name or ''}".strip())
        self.db.set_setting("userbot_phone", f"+{me.phone}")
        self.db.set_setting("userbot_active", "1")
        return "OK"

    async def disconnect(self):
        self.db.set_setting("userbot_active", "0")
        if self.client:
            await self.client.disconnect()
            self.client = None
        # Xoá file session để đăng xuất thực sự
        for ext in [".session", ".session-journal"]:
            p = Path(str(SESSION_FILE) + ext)
            if p.exists():
                p.unlink(missing_ok=True)

    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected()

    # ==================================================================
    # DÒ DANH SÁCH NHÓM
    # ==================================================================
    async def scan_groups(self, force: bool = False) -> list[dict]:
        """
        Quét tất cả dialog (hội thoại) của tài khoản và trả về danh sách nhóm/kênh.
        Cache kết quả trong 5 phút để tránh spam API.
        """
        if (
            not force
            and self._scan_cache
            and self._scan_at
            and (datetime.now() - self._scan_at) < timedelta(minutes=5)
        ):
            return self._scan_cache

        if not self.is_connected():
            return []

        groups: list[dict] = []
        try:
            async for dialog in self.client.iter_dialogs():
                entity = dialog.entity
                is_group = isinstance(entity, (types.Chat, types.Channel))
                is_channel_or_megagroup = isinstance(entity, types.Channel) and (
                    entity.megagroup or entity.gigagroup or not entity.broadcast
                )
                is_basic_group = isinstance(entity, types.Chat)

                if not (is_channel_or_megagroup or is_basic_group):
                    continue
                if isinstance(entity, types.Chat) and getattr(entity, "deactivated", False):
                    continue
                # Bỏ qua nếu tài khoản bị kick hoặc left
                if getattr(entity, "left", False):
                    continue

                chat_id = dialog.id
                # Telegram lưu megagroup với ID âm (supergroup -100xxx)
                if isinstance(entity, types.Channel):
                    full_id = int(f"-100{entity.id}")
                else:
                    full_id = -abs(entity.id)

                title = getattr(entity, "title", str(full_id))
                username = getattr(entity, "username", None)
                members = getattr(entity, "participants_count", None) or getattr(entity, "members_count", None) or 0

                groups.append(
                    {
                        "chat_id": full_id,
                        "title": title,
                        "username": username,
                        "members": members,
                        "in_db": self.db.get_group(full_id) is not None,
                        "active": bool(
                            self.db.get_group(full_id) and self.db.get_group(full_id).get("active", 0)
                        ),
                    }
                )

            self._scan_cache = sorted(groups, key=lambda g: (-g["members"], g["title"]))
            self._scan_at = datetime.now()
        except Exception as e:
            logger.error(f"[Userbot] Lỗi khi quét nhóm: {e}")

        return self._scan_cache

    # ==================================================================
    # GỬI BÀI VIA USERBOT
    # ==================================================================
    async def forward_post(self, post: dict, group_chat_id: int) -> tuple[bool, str]:
        """
        Chuyển tiếp bài viết từ storage (chat cá nhân với Bot) sang nhóm đích.
        Tự động xử lý FloodWait và các lỗi thường gặp.
        """
        if not self.is_connected():
            return False, "Userbot chưa kết nối"
        try:
            mode = post.get("send_mode", "forward")
            src_chat = post["storage_chat_id"]
            src_msg_id = post["storage_message_id"]

            if mode == "forward":
                await self.client.forward_messages(
                    entity=group_chat_id,
                    messages=src_msg_id,
                    from_peer=src_chat,
                )
            else:
                # Copy (ẩn nguồn): lấy message rồi gửi lại
                msgs = await self.client.get_messages(src_chat, ids=src_msg_id)
                if not msgs:
                    return False, "Không tìm thấy bài viết gốc"
                msg = msgs
                if hasattr(msg, "media") and msg.media:
                    await self.client.send_file(
                        group_chat_id,
                        msg.media,
                        caption=msg.message or "",
                    )
                else:
                    await self.client.send_message(group_chat_id, msg.message or "")

            return True, "OK"

        except FloodWaitError as e:
            wait_secs = e.seconds
            logger.warning(f"[Userbot] FloodWait {wait_secs}s khi gửi vào nhóm {group_chat_id}")
            return False, f"FloodWait {wait_secs}s — Tự động thử lại sau."
        except ChatWriteForbiddenError:
            return False, "Không có quyền gửi tin nhắn vào nhóm này"
        except UserBannedInChannelError:
            return False, "Tài khoản bị cấm trong nhóm này"
        except ChannelPrivateError:
            return False, "Nhóm là private và tài khoản không có quyền truy cập"
        except PeerFloodError:
            return False, "Tài khoản bị giới hạn tạm thời bởi Telegram (PeerFlood)"
        except Exception as e:
            return False, str(e)[:120]


# Singleton dùng chung trong toàn bộ ứng dụng
_manager_instance: Optional[UserbotManager] = None


def get_manager(db=None) -> UserbotManager:
    global _manager_instance
    if _manager_instance is None:
        if db is None:
            raise RuntimeError("Phải truyền db khi khởi tạo lần đầu")
        _manager_instance = UserbotManager(db)
    return _manager_instance
