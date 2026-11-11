"""
userbot.py — Module Telethon Userbot
Dùng tài khoản Telegram cá nhân (đăng nhập bằng Quét QR hoặc SĐT + OTP) để:
  1. Gửi bài trực tiếp vào bất kỳ nhóm nào mà KHÔNG CẦN thêm Bot vào nhóm!
  2. Tự động tham gia nhóm công khai (@username) nếu tài khoản chưa vào nhóm.
  3. Dò danh sách tất cả nhóm đang tham gia → gửi về Bot để bật/tắt 1 chạm.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, functions, types
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    ChannelPrivateError,
    PeerFloodError,
)

load_dotenv()
logger = logging.getLogger(__name__)

SESSION_FILE = Path(os.environ.get("SESSION_PATH", str(Path(__file__).parent / "userbot_session")))

# Built-in Telegram Desktop official API credentials (dùng tự động nếu người dùng chưa điền .env)
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


def get_api_credentials() -> tuple[int, str]:
    raw_id = os.getenv("TG_API_ID", "").strip()
    raw_hash = os.getenv("TG_API_HASH", "").strip()
    try:
        api_id = int(raw_id)
    except ValueError:
        api_id = 0

    if not api_id or not raw_hash or "YOUR" in raw_hash.upper():
        return DEFAULT_API_ID, DEFAULT_API_HASH
    return api_id, raw_hash


class UserbotManager:
    def __init__(self, db):
        self.db = db
        self.client: Optional[TelegramClient] = None
        self._scan_cache: list[dict] = []
        self._scan_at: Optional[datetime] = None
        self._phone: str = ""
        self._phone_code_hash: str = ""
        self._dialogs_loaded: bool = False
        self._qr_task: Optional[asyncio.Task] = None

    # ==================================================================
    # KHỞI TẠO & ĐĂNG NHẬP CLIENT
    # ==================================================================
    def _build_client(self) -> TelegramClient:
        api_id, api_hash = get_api_credentials()
        return TelegramClient(
            str(SESSION_FILE),
            api_id,
            api_hash,
            device_model="Telegram Desktop",
            system_version="Windows 10",
            app_version="4.16.8 x64",
            lang_code="vi",
            system_lang_code="vi",
        )

    async def _save_logged_in_info(self):
        me = await self.client.get_me()
        full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "Tài khoản Telegram"
        phone_str = f"+{me.phone}" if getattr(me, "phone", None) else "Đã liên kết QR"
        self.db.set_setting("userbot_name", full_name)
        self.db.set_setting("userbot_phone", phone_str)
        self.db.set_setting("userbot_active", "1")
        logger.info(f"[Userbot] Đã kết nối tài khoản: {full_name} ({phone_str})")
        # Tải danh sách hội thoại ngầm để cache entity ID các nhóm
        try:
            await self.client.get_dialogs(limit=200)
            self._dialogs_loaded = True
        except Exception as e:
            logger.warning(f"[Userbot] Không thể preload dialogs: {e}")

    async def start_silent(self) -> bool:
        """Thử kết nối lại bằng session đã lưu khi khởi động Bot."""
        try:
            if self.client and self.client.is_connected():
                if await self.client.is_user_authorized():
                    return True
            self.client = self._build_client()
            await self.client.connect()
            if await self.client.is_user_authorized():
                await self._save_logged_in_info()
                return True
            await self.client.disconnect()
            self.client = None
            return False
        except Exception as e:
            logger.warning(f"[Userbot] Không thể kết nối silent: {e}")
            return False

    async def is_authorized_and_ready(self) -> bool:
        if not self.client or not self.client.is_connected():
            return False
        try:
            return await self.client.is_user_authorized()
        except Exception:
            return False

    def is_connected(self) -> bool:
        return (
            self.client is not None
            and self.client.is_connected()
            and self.db.get_setting("userbot_active", "0") == "1"
        )

    async def create_qr_login(self):
        """Khởi tạo phiên đăng nhập bằng Mã QR, trả về (qr_login_obj, png_bytes)."""
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = self._build_client()
        await self.client.connect()
        qr_login = await self.client.qr_login()

        # Tạo ảnh PNG QR để gửi thẳng vào chat Telegram cho người dùng quét
        png_bytes = None
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(
                    "https://api.qrserver.com/v1/create-qr-code/",
                    params={"size": "340x340", "margin": "16", "data": qr_login.url},
                )
                if resp.status_code == 200:
                    png_bytes = resp.content
        except Exception as e:
            logger.warning(f"[Userbot] Lỗi tạo ảnh QR: {e}")

        return qr_login, png_bytes

    async def request_code(self, phone: str) -> str:
        """Gửi yêu cầu mã OTP đến SĐT."""
        self._phone = re.sub(r"[^\d+]", "", phone.strip())
        if not self._phone.startswith("+"):
            if self._phone.startswith("0"):
                self._phone = "+84" + self._phone[1:]
            else:
                self._phone = "+" + self._phone
        try:
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self.client = self._build_client()
            await self.client.connect()
            result = await self.client.send_code_request(self._phone)
            self._phone_code_hash = result.phone_code_hash
            return "CODE_SENT"
        except Exception as e:
            return f"ERROR:{e}"

    async def sign_in(self, code: str, password: str = "") -> str:
        """Đăng nhập bằng mã OTP (cho phép nhập cách chữ số '1 2 3 4 5' để tránh bị Telegram huỷ mã)."""
        if not self.client:
            return "NO_CLIENT"
        clean_code = re.sub(r"\D", "", code or "")
        try:
            if password:
                await self.client.sign_in(password=password)
            else:
                await self.client.sign_in(
                    phone=self._phone,
                    code=clean_code,
                    phone_code_hash=self._phone_code_hash,
                )
        except Exception as exc:
            exc_name = type(exc).__name__
            if "SessionPasswordNeeded" in exc_name:
                if not password:
                    return "NEED_2FA"
                try:
                    await self.client.sign_in(password=password)
                except Exception as e2:
                    return f"2FA_WRONG:{e2}"
            else:
                return f"ERROR:{exc}"

        await self._save_logged_in_info()
        return "OK"

    async def finish_2fa_after_qr(self, password: str) -> str:
        """Hoàn tất mật khẩu 2FA sau khi quét mã QR."""
        if not self.client:
            return "NO_CLIENT"
        try:
            await self.client.sign_in(password=password)
            await self._save_logged_in_info()
            return "OK"
        except Exception as e:
            return f"2FA_WRONG:{e}"

    async def disconnect(self):
        self.db.set_setting("userbot_active", "0")
        self._dialogs_loaded = False
        if self.client:
            try:
                await self.client.log_out()
            except Exception:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self.client = None
        for ext in ["", ".session", ".session-journal"]:
            p = Path(str(SESSION_FILE) + ext)
            if p.exists() and p.is_file():
                p.unlink(missing_ok=True)

    # ==================================================================
    # DÒ DANH SÁCH NHÓM
    # ==================================================================
    async def scan_groups(self, force: bool = False) -> list[dict]:
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
                is_channel_or_megagroup = isinstance(entity, types.Channel) and (
                    getattr(entity, "megagroup", False)
                    or getattr(entity, "gigagroup", False)
                    or not getattr(entity, "broadcast", True)
                )
                is_basic_group = isinstance(entity, types.Chat)

                if not (is_channel_or_megagroup or is_basic_group):
                    continue
                if isinstance(entity, types.Chat) and getattr(entity, "deactivated", False):
                    continue
                if getattr(entity, "left", False):
                    continue

                if isinstance(entity, types.Channel):
                    full_id = int(f"-100{entity.id}")
                else:
                    full_id = -abs(entity.id)

                title = getattr(entity, "title", str(full_id))
                username = getattr(entity, "username", None)
                members = (
                    getattr(entity, "participants_count", None)
                    or getattr(entity, "members_count", None)
                    or 0
                )

                db_grp = self.db.get_group(full_id)
                groups.append(
                    {
                        "chat_id": full_id,
                        "title": title,
                        "username": username,
                        "members": members,
                        "in_db": db_grp is not None,
                        "active": bool(db_grp and db_grp.get("active", 0)),
                    }
                )

            self._dialogs_loaded = True
            self._scan_cache = sorted(groups, key=lambda g: (-g["members"], g["title"]))
            self._scan_at = datetime.now()
        except Exception as e:
            logger.error(f"[Userbot] Lỗi khi quét nhóm: {e}")

        return self._scan_cache

    # ==================================================================
    # GỬI BÀI TRỰC TIẾP BẰNG NICK CÁ NHÂN (KHÔNG CẦN ADD BOT VÀO NHÓM)
    # ==================================================================
    async def _resolve_group_entity(self, group: dict):
        group_chat_id = int(group["chat_id"])
        username = (group.get("username") or "").strip().lstrip("@")

        if not self._dialogs_loaded:
            try:
                await self.client.get_dialogs(limit=200)
                self._dialogs_loaded = True
            except Exception:
                pass

        try:
            return await self.client.get_entity(group_chat_id)
        except Exception:
            if username:
                entity = await self.client.get_entity(f"@{username}")
                # Tự động tham gia nhóm công khai nếu nick chưa tham gia
                if isinstance(entity, types.Channel) and getattr(entity, "left", False):
                    try:
                        await self.client(functions.channels.JoinChannelRequest(entity))
                    except Exception:
                        pass
                return entity
            raise

    async def send_post(
        self,
        post: dict,
        group: dict,
        delete_old_msg_id: Optional[int] = None,
    ) -> tuple[bool, int, str]:
        """
        Gửi bài viết (ảnh/video + nội dung text) vào nhóm bằng tài khoản cá nhân.
        Trả về (success, sent_message_id, detail_message).
        """
        if not self.is_connected():
            if not await self.start_silent():
                return False, 0, "Userbot chưa kết nối"

        try:
            entity = await self._resolve_group_entity(group)

            # Xoá bài cũ nếu bật chế độ tự dọn bài cũ
            if delete_old_msg_id:
                try:
                    await self.client.delete_messages(entity, [int(delete_old_msg_id)])
                except Exception:
                    pass

            text = (post.get("content_text") or "").strip()
            if not text:
                # Fallback cho bài cũ tạo trước khi có cột content_text
                preview = (post.get("preview_text") or "").strip()
                if preview.startswith("[CÓ ẢNH] "):
                    preview = preview[len("[CÓ ẢNH] "):]
                text = preview

            media_path = (post.get("media_path") or "").strip()

            async def _do_send():
                if media_path and Path(media_path).exists():
                    return await self.client.send_file(
                        entity,
                        file=media_path,
                        caption=text,
                    )
                if not text:
                    raise ValueError("Bài đăng trống nội dung")
                return await self.client.send_message(entity, text, link_preview=True)

            try:
                sent_msg = await _do_send()
            except ChatWriteForbiddenError:
                # Nếu chưa join nhóm công khai → thử tự động join rồi gửi lại!
                if isinstance(entity, types.Channel):
                    await self.client(functions.channels.JoinChannelRequest(entity))
                    sent_msg = await _do_send()
                else:
                    raise

            msg_id = getattr(sent_msg, "id", 0) or 0
            return True, msg_id, "Đã gửi bằng Userbot"

        except FloodWaitError as e:
            logger.warning(f"[Userbot] FloodWait {e.seconds}s khi gửi vào {group.get('title')}")
            return False, 0, f"Telegram bắt chờ {e.seconds}s chống spam"
        except ChatWriteForbiddenError:
            return False, 0, "Nhóm khoá chat hoặc nick của bạn chưa tham gia nhóm này"
        except UserBannedInChannelError:
            return False, 0, "Nick của bạn bị cấm chat trong nhóm này"
        except ChannelPrivateError:
            return False, 0, "Nhóm riêng tư — nick của bạn chưa tham gia nhóm này"
        except PeerFloodError:
            return False, 0, "Nick bị Telegram giới hạn tạm thời (PeerFlood)"
        except Exception as e:
            logger.error(f"[Userbot] Lỗi gửi bài vào {group.get('title')}: {e}")
            return False, 0, str(e)[:120]


# Singleton dùng chung trong toàn bộ ứng dụng
_manager_instance: Optional[UserbotManager] = None


def get_manager(db=None) -> UserbotManager:
    global _manager_instance
    if _manager_instance is None:
        if db is None:
            raise RuntimeError("Phải truyền db khi khởi tạo lần đầu")
        _manager_instance = UserbotManager(db)
    return _manager_instance
