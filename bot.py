import os
import io
import html
import re
import logging
import asyncio
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from database import Database
from scheduler import SmartScheduler
from userbot import get_manager, UserbotManager

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database()
scheduler = SmartScheduler(db)
MEDIA_DIR = Path(__file__).parent / "media"
MEDIA_DIR.mkdir(exist_ok=True)


async def safe_edit_message(q, text: str, reply_markup=None, disable_web_page_preview: bool = True):
    """Chỉnh sửa tin nhắn an toàn, bỏ qua lỗi 'Message is not modified' của Telegram."""
    try:
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        try:
            await q.message.reply_text(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except Exception:
            pass

# ─── Menu bàn phím chính (thay đổi theo trạng thái) ─────────────────────────
MENU_SETUP = ReplyKeyboardMarkup(
    [[KeyboardButton("👥 Thêm nhóm đích"), KeyboardButton("🔍 Xem danh sách nhóm")]],
    resize_keyboard=True,
)
MENU_MAIN = ReplyKeyboardMarkup(
    [
        [KeyboardButton("✍️ Soạn bài đăng mới"), KeyboardButton("👥 Quản lý nhóm")],
        [KeyboardButton("📋 Danh sách bài đăng"), KeyboardButton("📊 Nhật ký & Tiến độ")],
        [KeyboardButton("⚙️ Cài đặt"), KeyboardButton("🔑 Userbot")],
    ],
    resize_keyboard=True,
)


def get_menu_keyboard():
    """Trả về đúng bàn phím tùy theo đã có nhóm chưa."""
    groups = db.get_groups(active_only=True)
    return MENU_MAIN if groups else MENU_SETUP


# ─── Helpers ────────────────────────────────────────────────────────────────
def _try_parse_group_link(text: str) -> str | None:
    text = text.strip()
    if re.fullmatch(r"@[a-zA-Z]\w{3,}", text):
        return text
    m = re.search(r"(?:t\.me|telegram\.me)/([a-zA-Z][a-zA-Z0-9_]{3,})", text)
    if m:
        return f"@{m.group(1)}"
    if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{4,}", text):
        return f"@{text}"
    return None


def _try_extract_group_from_forward(message) -> dict | None:
    fo = getattr(message, "forward_origin", None)
    if not fo:
        return None
    if type(fo).__name__ == "MessageOriginChat":
        chat = getattr(fo, "sender_chat", None) or getattr(fo, "chat", None)
        if chat and getattr(chat, "type", "") in ("group", "supergroup"):
            return {
                "chat_id": chat.id,
                "title": getattr(chat, "title", str(chat.id)),
                "username": getattr(chat, "username", None),
            }
    return None


def _group_added_reply(result: dict, title: str, chat_id: int, extra: str = "") -> str:
    active_groups = db.get_groups(active_only=True)
    max_g = int(db.get_setting("max_groups", "3"))
    if result["status"] == "updated":
        status_txt = "🔄 Thông tin đã được cập nhật."
    elif result["active"]:
        status_txt = f"🟢 Đã <b>BẬT</b> nhận bài! ({len(active_groups)}/{max_g} nhóm)"
    else:
        status_txt = f"⚪ Đã lưu, đang <b>TẮT</b> (đủ {max_g} nhóm rồi). Vào <b>👥 Quản lý nhóm</b> để bật."
    return (
        f"✅ <b>Đã lưu nhóm!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👥 Tên: <b>{html.escape(title)}</b>\n"
        f"🆔 ID: <code>{chat_id}</code>\n"
        f"{extra}"
        f"{status_txt}"
    )


def build_post_detail_text(post_id: int) -> str:
    post = db.get_post(post_id)
    if not post:
        return "❌ Bài viết không tồn tại."
    status_badge = "🟢 ĐANG CHẠY" if post["active"] else "⏸ TẠM DỪNG"
    max_runs = int(post["max_runs"])
    max_runs_str = f"{max_runs} lần/nhóm" if max_runs > 0 else "♾ Vô hạn"
    schedule_str = (
        f"🕒 Giờ cố định: <b>{html.escape(post['fixed_times'])}</b>"
        if post.get("fixed_times")
        else f"⏱ Mỗi <b>{post['interval_minutes']} phút</b>"
    )
    mode_str = "↪️ Forward (giữ nguồn)" if post["send_mode"] == "forward" else "📋 Copy (ẩn nguồn)"
    progress_list = db.get_post_progress(post_id)
    prog_lines = []
    for p in progress_list:
        done = p["runs_done"]
        lim = str(max_runs) if max_runs > 0 else "∞"
        last = ""
        if p.get("last_sent_at"):
            try:
                dt = datetime.fromisoformat(p["last_sent_at"])
                last = f" ({dt.strftime('%H:%M %d/%m')})"
            except Exception:
                pass
        icon = "✅" if (max_runs > 0 and done >= max_runs) else "📤"
        prog_lines.append(f"  {icon} <b>{html.escape(p['group_title'])}</b>: <code>{done}/{lim}</code>{last}")

    return (
        f"📌 <b>BÀI #{post['id']}</b> — {status_badge}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>{html.escape(post['title'])}</b>\n"
        f"🔄 {mode_str} | 🎯 {max_runs_str}\n"
        f"{schedule_str}\n\n"
        f"📈 <b>Tiến độ:</b>\n"
        + ("\n".join(prog_lines) if prog_lines else "  <i>⚠️ Chưa có nhóm nào đang bật</i>")
        + (f"\n\n📝 <i>{html.escape(post.get('preview_text') or '')[:120]}</i>" if post.get("preview_text") else "")
    )


def build_post_keyboard(post_id: int) -> InlineKeyboardMarkup:
    post = db.get_post(post_id)
    if not post:
        return InlineKeyboardMarkup([])
    toggle = (
        InlineKeyboardButton("⏸ Tạm dừng", callback_data=f"post_toggle:{post_id}")
        if post["active"]
        else InlineKeyboardButton("▶️ Bật chạy", callback_data=f"post_toggle:{post_id}")
    )
    mode_lbl = "🔄 Đổi → Copy (ẩn nguồn)" if post["send_mode"] == "forward" else "🔄 Đổi → Forward"
    return InlineKeyboardMarkup([
        [toggle],
        [
            InlineKeyboardButton("1×", callback_data=f"post_runs:{post_id}:1"),
            InlineKeyboardButton("3×", callback_data=f"post_runs:{post_id}:3"),
            InlineKeyboardButton("5×", callback_data=f"post_runs:{post_id}:5"),
            InlineKeyboardButton("10×", callback_data=f"post_runs:{post_id}:10"),
            InlineKeyboardButton("♾", callback_data=f"post_runs:{post_id}:0"),
        ],
        [InlineKeyboardButton("✏️ Nhập số lần tùy chỉnh", callback_data=f"post_ask_runs:{post_id}")],
        [
            InlineKeyboardButton("10p", callback_data=f"post_int:{post_id}:10"),
            InlineKeyboardButton("15p", callback_data=f"post_int:{post_id}:15"),
            InlineKeyboardButton("30p", callback_data=f"post_int:{post_id}:30"),
            InlineKeyboardButton("60p", callback_data=f"post_int:{post_id}:60"),
            InlineKeyboardButton("120p", callback_data=f"post_int:{post_id}:120"),
        ],
        [
            InlineKeyboardButton("✏️ Nhập số phút", callback_data=f"post_ask_int:{post_id}"),
            InlineKeyboardButton("🕒 Hẹn giờ cụ thể", callback_data=f"post_ask_time:{post_id}"),
        ],
        [InlineKeyboardButton(mode_lbl, callback_data=f"post_mode:{post_id}")],
        [
            InlineKeyboardButton("👁 Xem bài gốc", callback_data=f"post_preview:{post_id}"),
            InlineKeyboardButton("🚀 Gửi thử ngay", callback_data=f"post_sendnow:{post_id}"),
        ],
        [
            InlineKeyboardButton("♻️ Reset về 0", callback_data=f"post_reset:{post_id}"),
            InlineKeyboardButton("🗑 Xoá bài", callback_data=f"post_del:{post_id}"),
        ],
        [InlineKeyboardButton("⬅️ Danh sách bài", callback_data="menu_posts")],
    ])


# ─── UI các màn hình ─────────────────────────────────────────────────────────
async def show_setup_screen(update_or_query, context, is_callback=False):
    """Màn hình thiết lập nhóm (hiện khi chưa có nhóm nào)."""
    groups = db.get_groups(active_only=False)
    active = [g for g in groups if g["active"]]
    max_g = int(db.get_setting("max_groups", "3"))

    lines = [
        f"👥 <b>THIẾT LẬP NHÓM ĐÍCH ({len(active)}/{max_g} nhóm đã bật)</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if not groups:
        lines += [
            "Bạn chưa thêm nhóm nào. Hãy thêm <b>3 nhóm Telegram</b> mà bạn muốn Bot tự động đăng bài vào.\n",
            "📌 <b>3 cách thêm nhóm:</b>",
            "① Gửi link nhóm vào đây: <code>t.me/tengroup</code> hoặc <code>@tengroup</code>",
            "② Forward bất kỳ tin nhắn nào từ nhóm vào đây",
            "③ Thêm Bot vào nhóm rồi gõ <code>/addgroup</code> trong nhóm",
        ]
    else:
        for i, g in enumerate(groups, 1):
            icon = "🟢" if g["active"] else "⚪"
            lines.append(f"{i}. {icon} <b>{html.escape(g['title'])}</b> (<code>{g['chat_id']}</code>)")
        if len(active) < max_g:
            lines.append(f"\n➕ Còn chỗ cho <b>{max_g - len(active)}</b> nhóm nữa. Tiếp tục gửi link/forward!")
        else:
            lines.append(f"\n✅ <b>Đã đủ {max_g} nhóm!</b> Bấm <b>✍️ Soạn bài đăng mới</b> để bắt đầu.")

    kb = []
    for g in groups:
        kb.append([
            InlineKeyboardButton(
                f"{'🟢' if g['active'] else '⚪'} {g['title'][:26]}",
                callback_data=f"grp_toggle:{g['chat_id']}",
            ),
            InlineKeyboardButton("🗑", callback_data=f"grp_del:{g['chat_id']}"),
        ])

    if len(active) >= max_g:
        kb.append([InlineKeyboardButton("✍️ Soạn bài đăng ngay →", callback_data="compose_start")])
    kb.append([InlineKeyboardButton("🔄 Làm mới", callback_data="menu_groups")])

    markup = InlineKeyboardMarkup(kb)
    text = "\n".join(lines)
    if is_callback:
        await safe_edit_message(update_or_query, text, reply_markup=markup)
    else:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def show_compose_prompt(update_or_query, context, is_callback=False):
    """Màn hình soạn bài — hướng dẫn gửi ảnh+text."""
    groups = db.get_groups(active_only=True)

    # Liệt kê rõ từng nhóm sẽ nhận bài
    if groups:
        group_lines = "\n".join([
            f"   {'🟢'} {i+1}. <b>{html.escape(g['title'])}</b>"
            for i, g in enumerate(groups)
        ])
    else:
        group_lines = "   <i>⚠️ Chưa có nhóm nào! Vào 👥 Quản lý nhóm để thêm.</i>"

    text = (
        "✍️ <b>SOẠN BÀI ĐĂNG MỚI</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📢 <b>Bài sẽ được đăng vào {len(groups)} nhóm:</b>\n"
        f"{group_lines}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👇 <b>Gửi nội dung bài vào đây:</b>\n\n"
        "🖼 <b>Có ảnh + chú thích:</b>\n"
        "   → Gửi <b>ảnh</b> kèm <b>caption</b> (gõ nội dung vào ô Caption dưới ảnh)\n\n"
        "📝 <b>Chỉ text / link:</b>\n"
        "   → Gửi <b>tin nhắn văn bản</b> vào đây\n\n"
        "↪️ <b>Forward bài từ kênh/nhóm khác:</b>\n"
        "   → Chuyển tiếp (Forward) tin nhắn vào đây\n\n"
        "<i>⚠️ Sau khi gửi, bot sẽ hỏi xác nhận trước khi lưu!</i>"
    )
    kb = [[InlineKeyboardButton("❌ Huỷ soạn bài", callback_data="compose_cancel")]]
    markup = InlineKeyboardMarkup(kb)
    if is_callback:
        await safe_edit_message(update_or_query, text, reply_markup=markup)
    else:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def show_posts_list(update_or_query, context, is_callback=False):
    posts = db.get_posts(active_only=False)
    groups = db.get_groups(active_only=True)
    if not posts:
        text = (
            "📭 <b>CHƯA CÓ BÀI NÀO</b>\n\n"
            "👉 Bấm <b>✍️ Soạn bài đăng mới</b> để tạo bài quảng cáo đầu tiên!"
        )
        kb = [[InlineKeyboardButton("✍️ Soạn bài đăng mới", callback_data="compose_start")]]
        if is_callback:
            await safe_edit_message(update_or_query, text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return

    active_cnt = sum(1 for p in posts if p["active"])
    lines = [
        f"📋 <b>DANH SÁCH BÀI ({len(posts)} bài | {active_cnt} đang chạy)</b>",
        f"👥 Nhóm đang nhận: <b>{len(groups)}/3</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    kb = []
    for p in posts:
        progress = db.get_post_progress(p["id"])
        max_r = int(p["max_runs"])
        lim = str(max_r) if max_r > 0 else "∞"
        prog = ", ".join([f"{pr['runs_done']}/{lim}" for pr in progress]) if progress else "—"
        icon = "🟢" if p["active"] else "⏸"
        sched = p["fixed_times"] if p.get("fixed_times") else f"{p['interval_minutes']}p"
        # Hiển thị ảnh/text icon
        type_icon = "🖼" if p.get("has_photo") else "📝"
        lines.append(f"{icon}{type_icon} <b>#{p['id']}</b> {html.escape(p['title'])} | {sched} | [{prog}]")
        kb.append([InlineKeyboardButton(
            f"{icon}{type_icon} #{p['id']}: {p['title'][:30]} ({sched})",
            callback_data=f"post_view:{p['id']}",
        )])

    kb += [
        [
            InlineKeyboardButton("▶️ Bật tất cả", callback_data="posts_start_all"),
            InlineKeyboardButton("⏸ Dừng tất cả", callback_data="posts_stop_all"),
        ],
        [
            InlineKeyboardButton("✍️ Soạn bài mới", callback_data="compose_start"),
            InlineKeyboardButton("🔄 Làm mới", callback_data="menu_posts"),
        ],
    ]
    text = "\n".join(lines) + "\n\n👇 <i>Bấm vào bài để chỉnh lịch và số lần đăng:</i>"
    if is_callback:
        await safe_edit_message(update_or_query, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))


async def show_settings_menu(update_or_query, context, is_callback=False):
    stagger = db.get_setting("stagger_minutes", "5")
    cooldown = db.get_setting("min_group_cooldown", "5")
    del_old = db.get_setting("delete_previous_ad", "0") == "1"
    text = (
        "⚙️ <b>CÀI ĐẶT CHỐNG TRÙNG</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Lệch tối thiểu khi gửi cùng 1 bài vào 2 nhóm: <code>{stagger} phút</code>\n"
        f"⏳ Nghỉ tối thiểu giữa 2 bài trong 1 nhóm: <code>{cooldown} phút</code>\n"
        f"🧹 Xoá bài cũ trước khi đăng bài mới: <b>{'🟢 BẬT' if del_old else '⚪ TẮT'}</b>"
    )
    kb = [
        [InlineKeyboardButton(f"⏱ Lệch nhóm: {stagger}p → Đổi", callback_data="set_cycle_stagger")],
        [InlineKeyboardButton(f"⏳ Nghỉ trong nhóm: {cooldown}p → Đổi", callback_data="set_cycle_cooldown")],
        [InlineKeyboardButton(f"🧹 Xoá bài cũ: {'🟢 BẬT' if del_old else '⚪ TẮT'}", callback_data="set_toggle_delold")],
    ]
    if is_callback:
        await safe_edit_message(update_or_query, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))


async def show_logs(update_or_query, context, is_callback=False):
    logs = db.get_recent_logs(12)
    if not logs:
        text = "📊 <b>NHẬT KÝ</b>\n━━━━━━━━━━━━━━━━━━\n<i>Chưa có lịch sử gửi bài.</i>"
    else:
        lines = ["📊 <b>12 LẦN GỬI GẦN NHẤT</b>", "━━━━━━━━━━━━━━━━━━"]
        for lg in logs:
            icon = "✅" if lg["status"] == "SUCCESS" else "❌"
            t = lg["sent_at"][11:16] if len(lg["sent_at"]) >= 16 else ""
            lines.append(f"{icon} <code>{t}</code> Bài <b>#{lg['post_id']}</b> → <b>{html.escape(lg['group_title'] or '')}</b> <i>{html.escape(lg['detail'] or '')}</i>")
        text = "\n".join(lines)
    kb = [[InlineKeyboardButton("🔄 Làm mới", callback_data="menu_logs")]]
    if is_callback:
        await safe_edit_message(update_or_query, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))


async def show_userbot_status(update_or_query, context, is_callback=False):
    ub: UserbotManager = get_manager(db)
    connected = ub.is_connected()
    name = db.get_setting("userbot_name", "")
    phone = db.get_setting("userbot_phone", "")

    if connected:
        text = (
            f"🔑 <b>USERBOT ĐÃ KẾT NỐI (GỬI KHÔNG CẦN ADD BOT)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 Nick: <b>{html.escape(name)}</b> ({html.escape(phone)})\n\n"
            f"✅ <b>Chế độ Userbot đang BẬT:</b>\n"
            f"• Tự động đăng bài bằng chính nick cá nhân của bạn vào các nhóm đã chọn.\n"
            f"• <b>KHÔNG CẦN</b> thêm Bot vào nhóm của người khác!\n"
            f"• Có thể bấm <b>🔍 Dò danh sách nhóm</b> để tick chọn nhanh 3 nhóm trong nick."
        )
        kb = [
            [InlineKeyboardButton("🔍 Dò & Chọn nhóm từ nick này", callback_data="ub_scan")],
            [InlineKeyboardButton("🚪 Đăng xuất nick", callback_data="ub_logout")],
        ]
    else:
        text = (
            "🔑 <b>ĐĂNG NHẬP NICK CÁ NHÂN (USERBOT)</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>Tại sao người khác gửi vào nhóm không cần add Bot?</b>\n"
            "Vì họ đăng nhập <b>nick Telegram cá nhân</b> vào tool! Khi kết nối ở đây:\n"
            "• Nick của bạn tự động rải bài vào mọi nhóm bạn tham gia\n"
            "• <b>Không cần quyền Admin nhóm, không cần thêm @bot vào nhóm!</b>\n"
            "• Tự động quét danh sách toàn bộ nhóm trong nick để tick chọn\n\n"
            "👇 <b>Chọn 1 trong 2 cách đăng nhập cực nhanh:</b>"
        )
        kb = [
            [InlineKeyboardButton("📷 Quét mã QR bằng ĐT (Khuyên dùng - 5s)", callback_data="ub_qr_login")],
            [InlineKeyboardButton("📱 Đăng nhập bằng SĐT + Mã OTP", callback_data="ub_login_start")],
        ]

    markup = InlineKeyboardMarkup(kb)
    if is_callback:
        await safe_edit_message(update_or_query, text, reply_markup=markup, disable_web_page_preview=True)
    else:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


async def show_group_scan_results(query, context, page=0, force_scan=False):
    ub: UserbotManager = get_manager(db)
    groups = await ub.scan_groups(force=force_scan)
    if not groups:
        await safe_edit_message(
            query,
            "⚠️ Không tìm thấy nhóm nào trong nick này.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="ub_status")]]),
        )
        return
    PAGE = 8
    total_pages = max(1, (len(groups) + PAGE - 1) // PAGE)
    page = max(0, min(page, total_pages - 1))
    page_groups = groups[page * PAGE: (page + 1) * PAGE]
    active_ids = {g["chat_id"] for g in db.get_groups(active_only=True)}
    max_g = int(db.get_setting("max_groups", "3"))
    text = (
        f"🔍 <b>DÒ NHÓM ({len(groups)} nhóm) — Trang {page+1}/{total_pages}</b>\n"
        f"Đã chọn: <b>{len(active_ids)}/{max_g}</b> nhóm. Bấm để bật/tắt:"
    )
    kb = []
    for g in page_groups:
        cid = g["chat_id"]
        icon = "🟢" if cid in active_ids else "⚪"
        mem = f"{g['members']:,}" if g["members"] else "?"
        kb.append([InlineKeyboardButton(
            f"{icon} {g['title'][:30]} ({mem}TV)",
            callback_data=f"ub_grp_toggle:{cid}:{page}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"ub_scan_page:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"ub_scan_page:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([
        InlineKeyboardButton("🔄 Quét lại", callback_data="ub_scan"),
        InlineKeyboardButton("⬅️ Quay lại", callback_data="ub_status"),
    ])
    await safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(kb))


# ─── Lưu bài vừa soạn ────────────────────────────────────────────────────────
async def _save_composed_from_pending(bot, pending: dict) -> int:
    """Lưu bài soạn vào DB (kèm tải ảnh/video về máy để Userbot gửi không cần Bot). Trả về post_id."""
    raw_text = pending.get("content_text", "")
    file_id = pending.get("file_id")
    media_type = pending.get("media_type", "")
    ext = pending.get("ext", ".jpg")
    has_media = bool(file_id)

    clean = [l.strip() for l in raw_text.splitlines() if l.strip()]
    first = clean[0] if clean else ""
    type_prefix = "🖼 " if has_media else "📝 "
    title = f"{type_prefix}{first[:40]}" if first else f"{type_prefix}Bài lúc {datetime.now().strftime('%H:%M %d/%m')}"
    preview = raw_text[:200] + ("..." if len(raw_text) > 200 else "")
    if has_media:
        preview = f"[CÓ ẢNH] {preview}"[:200]

    post_id = db.add_post(
        title=title,
        preview_text=preview,
        storage_chat_id=pending["chat_id"],
        storage_message_id=pending["message_id"],
        forward_origin_title="Bài soạn trực tiếp",
        send_mode="copy",
        max_runs=3,
        interval_minutes=30,
        content_text=raw_text,
        media_path="",
        media_type=media_type,
    )

    if file_id:
        try:
            tg_file = await bot.get_file(file_id)
            dest = MEDIA_DIR / f"post_{post_id}{ext}"
            await tg_file.download_to_drive(custom_path=str(dest))
            db.update_post(post_id, media_path=str(dest))
        except Exception as e:
            logger.warning(f"Không thể tải media cho bài #{post_id}: {e}")

    db.ensure_progress_rows(post_id)
    return post_id


# ─── Handlers ────────────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        res = db.add_or_update_group(chat.id, chat.title or str(chat.id), chat.username)
        await update.message.reply_text(
            f"✅ Nhóm <b>{html.escape(chat.title or '')}</b> đã được lưu! "
            f"Trạng thái: {'🟢 Đang bật' if res['active'] else '⚪ Đang tắt'}",
            parse_mode="HTML",
        )
        return

    db.set_setting("admin_chat_id", str(chat.id))
    groups = db.get_groups(active_only=True)
    if not groups:
        welcome = (
            "👋 <b>CHÀO MỪNG! BOT TỰ ĐỘNG ĐĂNG BÀI QUẢNG CÁO</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🚀 Hãy bắt đầu bằng cách <b>thêm 3 nhóm Telegram</b> bạn muốn đăng bài vào.\n\n"
            "📌 <b>Cách thêm nhóm (chọn 1 trong 3 cách):</b>\n"
            "① Gửi link: <code>t.me/username</code> hoặc <code>@username</code>\n"
            "② Forward bất kỳ tin nhắn nào từ nhóm vào đây\n"
            "③ Thêm Bot vào nhóm → gõ <code>/addgroup</code>\n\n"
            "Sau khi đủ nhóm, Bot sẽ mở menu <b>Soạn & Đăng bài</b> cho bạn! 👇"
        )
        await update.message.reply_text(welcome, parse_mode="HTML", reply_markup=MENU_SETUP)
        await show_setup_screen(update, context)
    else:
        await update.message.reply_text(
            f"👋 Chào lại! Bot đang chạy với <b>{len(groups)}/3 nhóm</b>.\n"
            f"Bấm <b>✍️ Soạn bài đăng mới</b> để tạo bài hoặc quản lý bài đang chạy.",
            parse_mode="HTML",
            reply_markup=get_menu_keyboard(),
        )


async def addgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ("group", "supergroup", "channel"):
        res = db.add_or_update_group(chat.id, chat.title or str(chat.id), chat.username)
        await update.message.reply_text(
            f"✅ Nhóm <b>{html.escape(chat.title or '')}</b> đã lưu! "
            f"{'🟢 BẬT' if res['active'] else '⚪ TẮT'}",
            parse_mode="HTML",
        )
        admin_id = db.get_setting("admin_chat_id")
        if admin_id:
            try:
                await context.bot.send_message(
                    int(admin_id),
                    f"🔔 Nhóm mới: <b>{html.escape(chat.title or '')}</b> (<code>{chat.id}</code>)",
                    parse_mode="HTML",
                )
            except Exception:
                pass


async def handle_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    if update.message and update.message.new_chat_members:
        for m in update.message.new_chat_members:
            if m.id == context.bot.id:
                db.add_or_update_group(chat.id, chat.title or str(chat.id), chat.username)
                await update.message.reply_text(
                    f"🤖 Nhóm <b>{html.escape(chat.title or '')}</b> đã được lưu tự động!",
                    parse_mode="HTML",
                )


# ─── Xử lý tin nhắn trong private chat ──────────────────────────────────────
async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return

    db.set_setting("admin_chat_id", str(update.effective_chat.id))
    msg = update.message
    text = (msg.text or "").strip()
    ub: UserbotManager = get_manager(db)

    # 1. Nút menu cứng
    btn_map = {
        "✍️ Soạn bài đăng mới": lambda: show_compose_prompt(update, context),
        "👥 Quản lý nhóm": lambda: show_setup_screen(update, context),
        "👥 Thêm nhóm đích": lambda: show_setup_screen(update, context),
        "🔍 Xem danh sách nhóm": lambda: show_setup_screen(update, context),
        "📋 Danh sách bài đăng": lambda: show_posts_list(update, context),
        "📊 Nhật ký & Tiến độ": lambda: show_logs(update, context),
        "⚙️ Cài đặt": lambda: show_settings_menu(update, context),
        "🔑 Userbot": lambda: show_userbot_status(update, context),
    }
    if text in btn_map:
        context.user_data.pop("awaiting_input", None)
        # Chế độ soạn bài: set flag
        if text == "✍️ Soạn bài đăng mới":
            context.user_data["compose_mode"] = True
        await btn_map[text]()
        return

    awaiting = context.user_data.get("awaiting_input")

    # 2. Chế độ soạn bài — nhận bất kỳ nội dung nào (ảnh, text, forward)
    if context.user_data.get("compose_mode"):
        # Chờ nhận bài (ảnh+caption, text, forward bài QC)
        is_forward_from_group = bool(_try_extract_group_from_forward(msg))
        if is_forward_from_group:
            # Nếu forward từ nhóm trong chế độ soạn bài → thêm nhóm, không lưu bài
            context.user_data.pop("compose_mode", None)
            await _handle_add_group_from_forward(update, context, msg)
            return

        # --- BƯỚC XÁC NHẬN TRƯỚC KHI LƯU ---
        has_media = bool(msg.photo or msg.video or msg.animation or msg.document)
        media_lbl = "🖼 Có ảnh/video" if has_media else "📝 Chỉ text"
        raw_text  = msg.text or msg.caption or ""
        preview   = raw_text[:300] + ("..." if len(raw_text) > 300 else "")

        # Liệt kê nhóm sẽ nhận bài
        groups = db.get_groups(active_only=True)
        group_lines = "\n".join([
            f"   🟢 {i+1}. <b>{html.escape(g['title'])}</b>"
            for i, g in enumerate(groups)
        ])

        confirm_text = (
            "👀 <b>XEM TRƯỚC BÀI ĐĂNG</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📌 Loại: <b>{media_lbl}</b>\n\n"
            f"📢 Sẽ gửi vào <b>{len(groups)} nhóm:</b>\n"
            f"{group_lines or '   <i>⚠️ Chưa có nhóm!</i>'}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📝 <b>Nội dung:</b>\n"
            f"<i>{html.escape(preview) if preview else '(không có text)'}</i>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "✅ Bấm <b>XÁC NHẬN</b> để lưu và cài lịch\n"
            "🔁 Bấm <b>Soạn lại</b> để gửi lại bài khác"
        )

        # Trích xuất thông tin media nếu có để lưu không cần forward lại
        file_id = None
        media_type = ""
        ext = ".jpg"
        if msg.photo:
            file_id = msg.photo[-1].file_id
            media_type = "photo"
            ext = ".jpg"
        elif msg.video:
            file_id = msg.video.file_id
            media_type = "video"
            ext = ".mp4"
        elif msg.animation:
            file_id = msg.animation.file_id
            media_type = "animation"
            ext = ".mp4"
        elif msg.document:
            file_id = msg.document.file_id
            media_type = "document"
            ext = Path(msg.document.file_name or "file.bin").suffix or ".bin"

        # Lưu message tạm chờ xác nhận
        context.user_data["pending_compose"] = {
            "chat_id": update.effective_chat.id,
            "message_id": msg.message_id,
            "content_text": raw_text,
            "file_id": file_id,
            "media_type": media_type,
            "ext": ext,
        }
        context.user_data.pop("compose_mode", None)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ XÁC NHẬN ĐĂNG", callback_data="compose_confirm"),
                InlineKeyboardButton("🔁 Soạn lại", callback_data="compose_restart"),
            ],
            [InlineKeyboardButton("❌ Huỷ bỏ", callback_data="compose_cancel")],
        ])
        await msg.reply_text(confirm_text, parse_mode="HTML", reply_markup=kb)
        return

    # 3. Chờ nhập userbot OTP / 2FA
    if awaiting:
        mode = awaiting.get("mode", "")
        if mode in ("ub_phone", "ub_code", "ub_2fa", "ub_qr_2fa") and not getattr(msg, "forward_origin", None):
            if mode == "ub_phone":
                context.user_data.pop("awaiting_input", None)
                await msg.reply_text("⏳ Đang gửi mã OTP về Telegram của bạn...")
                res = await ub.request_code(text)
                if res == "CODE_SENT":
                    context.user_data["awaiting_input"] = {"mode": "ub_code"}
                    await msg.reply_text(
                        "📱 <b>OTP đã gửi!</b>\n\n"
                        "⚠️ <b>LƯU Ý QUAN TRỌNG:</b> Hãy gõ 5 số OTP <b>cách nhau bằng dấu cách</b> "
                        "(ví dụ mã là <code>12345</code> thì gõ: <code>1 2 3 4 5</code>) để Telegram không tự huỷ mã!",
                        parse_mode="HTML",
                    )
                else:
                    await msg.reply_text(f"❌ Lỗi: {html.escape(res)}", parse_mode="HTML")
                return
            if mode == "ub_code":
                await msg.reply_text("⏳ Đang xác minh...")
                res = await ub.sign_in(text)
                if res == "OK":
                    context.user_data.pop("awaiting_input", None)
                    await msg.reply_text(
                        f"🎉 <b>Đăng nhập Userbot thành công!</b>\n"
                        f"👤 Nick: <b>{html.escape(db.get_setting('userbot_name', ''))}</b>\n\n"
                        f"✅ Giờ Bot đã có thể gửi bài vào <b>mọi nhóm mà KHÔNG CẦN thêm Bot vào nhóm!</b>",
                        parse_mode="HTML",
                        reply_markup=get_menu_keyboard(),
                    )
                elif res == "NEED_2FA":
                    context.user_data["awaiting_input"] = {"mode": "ub_2fa"}
                    await msg.reply_text("🔒 Tài khoản có bật bảo mật 2 lớp (2FA). Hãy nhập mật khẩu 2FA vào đây:")
                else:
                    await msg.reply_text(
                        f"❌ {html.escape(res)}\n\n"
                        f"💡 Nhớ gõ cách từng số (ví dụ: <code>1 2 3 4 5</code>) hoặc dùng <b>📷 Quét mã QR</b> cho nhanh!",
                        parse_mode="HTML",
                    )
                return
            if mode in ("ub_2fa", "ub_qr_2fa"):
                context.user_data.pop("awaiting_input", None)
                res = await (ub.finish_2fa_after_qr(text) if mode == "ub_qr_2fa" else ub.sign_in("", password=text))
                if res == "OK":
                    await msg.reply_text(
                        f"🎉 <b>Đăng nhập Userbot thành công!</b>\n"
                        f"👤 Nick: <b>{html.escape(db.get_setting('userbot_name', ''))}</b>\n\n"
                        f"✅ Giờ Bot đã có thể gửi bài vào mọi nhóm mà không cần thêm Bot vào nhóm!",
                        parse_mode="HTML",
                        reply_markup=get_menu_keyboard(),
                    )
                else:
                    await msg.reply_text(f"❌ Mật khẩu 2FA sai: {html.escape(res)}", parse_mode="HTML")
                return

        # Nhập số cho bài viết
        post_id = awaiting.get("post_id")
        if mode == "runs" and post_id and not getattr(msg, "forward_origin", None):
            try:
                val = int(text)
                if val < 0:
                    raise ValueError()
                db.update_post(post_id, max_runs=val)
                context.user_data.pop("awaiting_input", None)
                await msg.reply_text(
                    f"✅ Số lần đăng: <b>{'Vô hạn' if val == 0 else str(val)}</b> / nhóm",
                    parse_mode="HTML",
                )
                await msg.reply_text(build_post_detail_text(post_id), parse_mode="HTML", reply_markup=build_post_keyboard(post_id))
            except ValueError:
                await msg.reply_text("⚠️ Nhập số nguyên hợp lệ (0 = vô hạn):")
            return

        if mode == "interval" and post_id and not getattr(msg, "forward_origin", None):
            try:
                val = int(text)
                if val < 1:
                    raise ValueError()
                db.update_post(post_id, interval_minutes=val, fixed_times="")
                context.user_data.pop("awaiting_input", None)
                await msg.reply_text(f"✅ Chu kỳ đăng: mỗi <b>{val} phút</b>", parse_mode="HTML")
                await msg.reply_text(build_post_detail_text(post_id), parse_mode="HTML", reply_markup=build_post_keyboard(post_id))
            except ValueError:
                await msg.reply_text("⚠️ Nhập số phút ≥ 1:")
            return

        if mode == "fixed_time" and post_id and not getattr(msg, "forward_origin", None):
            context.user_data.pop("awaiting_input", None)
            if text.lower() in ("xoa", "0", "clear"):
                db.update_post(post_id, fixed_times="")
                await msg.reply_text("✅ Đã xoá giờ cố định, chuyển về chu kỳ phút.", parse_mode="HTML")
            else:
                cleaned = []
                for part in text.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        cleaned.append(datetime.strptime(part, "%H:%M").strftime("%H:%M"))
                    except ValueError:
                        context.user_data["awaiting_input"] = awaiting
                        await msg.reply_text(f"⚠️ Giờ <code>{html.escape(part)}</code> không hợp lệ. Ví dụ: <code>08:00, 20:30</code>", parse_mode="HTML")
                        return
                db.update_post(post_id, fixed_times=", ".join(cleaned))
                await msg.reply_text(f"✅ Giờ cố định: <b>{', '.join(cleaned)}</b>", parse_mode="HTML")
            await msg.reply_text(build_post_detail_text(post_id), parse_mode="HTML", reply_markup=build_post_keyboard(post_id))
            return

    # 4. Phát hiện Forward từ nhóm → thêm nhóm đích
    if _try_extract_group_from_forward(msg):
        await _handle_add_group_from_forward(update, context, msg)
        return

    # 5. Phát hiện link / @username nhóm
    group_username = _try_parse_group_link(text) if text else None
    if group_username:
        await _handle_add_group_by_username(update, context, group_username)
        return

    # 6. Tin nhắn không rõ → gợi ý
    groups = db.get_groups(active_only=True)
    if not groups:
        await msg.reply_text(
            "💡 Hãy gửi <b>link nhóm</b> (ví dụ: <code>t.me/XSLfq</code>) hoặc <b>forward tin nhắn từ nhóm</b> để thêm nhóm đích.",
            parse_mode="HTML",
        )
    else:
        await msg.reply_text(
            "💡 Bấm <b>✍️ Soạn bài đăng mới</b> rồi gửi ảnh+caption hoặc text vào đây!",
            parse_mode="HTML",
            reply_markup=get_menu_keyboard(),
        )


async def _handle_add_group_from_forward(update, context, msg):
    info = _try_extract_group_from_forward(msg)
    result = db.add_or_update_group(info["chat_id"], info["title"], info.get("username"))
    reply = _group_added_reply(result, info["title"], info["chat_id"])
    groups = db.get_groups(active_only=True)
    max_g = int(db.get_setting("max_groups", "3"))
    if len(groups) >= max_g:
        reply += f"\n\n✅ <b>Đã đủ {max_g} nhóm!</b> Bấm <b>✍️ Soạn bài đăng mới</b> để bắt đầu."
    await msg.reply_text(reply, parse_mode="HTML", reply_markup=get_menu_keyboard())


async def _handle_add_group_by_username(update, context, group_username: str):
    msg = update.message
    await msg.reply_text(f"🔍 Đang tra cứu <code>{html.escape(group_username)}</code>...", parse_mode="HTML")
    try:
        chat_obj = await context.bot.get_chat(group_username)
        if chat_obj.type not in ("group", "supergroup"):
            await msg.reply_text(
                f"⚠️ <code>{html.escape(group_username)}</code> là <b>kênh</b>, không phải nhóm.\n"
                f"Nếu muốn forward bài từ kênh này, hãy vào kênh → Forward bài → gửi vào đây.",
                parse_mode="HTML",
            )
            return
        result = db.add_or_update_group(chat_obj.id, chat_obj.title or group_username, chat_obj.username)
        members = getattr(chat_obj, "member_count", None)
        mem_str = f"\n👤 Thành viên: <b>{members:,}</b>" if members else ""
        reply = _group_added_reply(result, chat_obj.title or group_username, chat_obj.id, extra=mem_str + "\n")
        groups = db.get_groups(active_only=True)
        max_g = int(db.get_setting("max_groups", "3"))
        if len(groups) >= max_g:
            reply += f"\n\n✅ <b>Đã đủ {max_g} nhóm!</b> Bấm <b>✍️ Soạn bài đăng mới</b> để bắt đầu."
        await msg.reply_text(reply, parse_mode="HTML", reply_markup=get_menu_keyboard())
    except Exception as e:
        err = str(e)
        hint = "Bot cần được thêm vào nhóm này HOẶC nhóm đang là private." if "Chat not found" in err or "Invalid" in err else err
        await msg.reply_text(
            f"❌ Không tìm được <code>{html.escape(group_username)}</code>.\n<i>{html.escape(hint)}</i>\n\n"
            f"💡 Thử cách khác: Vào nhóm → Forward bất kỳ tin nhắn vào đây.",
            parse_mode="HTML",
        )


# ─── Callback handler ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    ub: UserbotManager = get_manager(db)
    answered = False

    async def ans(text: str = "", show_alert: bool = False):
        nonlocal answered
        if not answered:
            answered = True
            try:
                await q.answer(text=text or None, show_alert=show_alert)
            except Exception:
                pass

    try:
        # Menu
        if data == "menu_posts":
            await ans()
            await show_posts_list(q, context, True)
            return
        if data == "menu_groups":
            await ans()
            await show_setup_screen(q, context, True)
            return
        if data == "menu_logs":
            await ans()
            await show_logs(q, context, True)
            return
        if data == "compose_start":
            await ans()
            context.user_data["compose_mode"] = True
            await show_compose_prompt(q, context, True)
            return
        if data == "compose_cancel":
            await ans("Đã huỷ soạn bài")
            context.user_data.pop("compose_mode", None)
            context.user_data.pop("pending_compose", None)
            await safe_edit_message(
                q,
                "❌ Đã huỷ soạn bài. Bấm <b>✍️ Soạn bài đăng mới</b> khi muốn soạn lại.",
            )
            return
        if data == "compose_restart":
            await ans()
            context.user_data.pop("pending_compose", None)
            context.user_data["compose_mode"] = True
            await show_compose_prompt(q, context, True)
            return
        if data == "compose_confirm":
            pending = context.user_data.pop("pending_compose", None)
            if not pending:
                await ans("⚠️ Không tìm thấy bài soạn! Hãy soạn lại.", show_alert=True)
                return
            await ans("✅ Đang lưu bài...")
            post_id = await _save_composed_from_pending(context.bot, pending)
            await safe_edit_message(
                q,
                f"✅ <b>Đã lưu bài #{post_id}!</b>\n\n"
                f"Bây giờ cài <b>số lần đăng</b> và <b>lịch đăng</b> bên dưới rồi bấm <b>▶️ Bật chạy</b>:",
            )
            await q.message.reply_text(
                build_post_detail_text(post_id),
                parse_mode="HTML",
                reply_markup=build_post_keyboard(post_id),
            )
            return
        if data == "posts_start_all":
            for p in db.get_posts():
                db.update_post(p["id"], active=1)
            await ans("▶️ Đã BẬT tất cả bài đăng!", show_alert=True)
            await show_posts_list(q, context, True)
            return
        if data == "posts_stop_all":
            for p in db.get_posts():
                db.update_post(p["id"], active=0)
            await ans("⏸ Đã DỪNG tất cả bài đăng!", show_alert=True)
            await show_posts_list(q, context, True)
            return

        # Settings
        if data == "set_cycle_stagger":
            opts = ["1", "3", "5", "10", "15"]
            curr = db.get_setting("stagger_minutes", "5")
            nxt = opts[(opts.index(curr) + 1) % len(opts)] if curr in opts else "5"
            db.set_setting("stagger_minutes", nxt)
            await ans(f"⏱ Lệch nhóm: {nxt} phút")
            await show_settings_menu(q, context, True)
            return
        if data == "set_cycle_cooldown":
            opts = ["1", "2", "5", "10", "15"]
            curr = db.get_setting("min_group_cooldown", "5")
            nxt = opts[(opts.index(curr) + 1) % len(opts)] if curr in opts else "5"
            db.set_setting("min_group_cooldown", nxt)
            await ans(f"⏳ Nghỉ trong nhóm: {nxt} phút")
            await show_settings_menu(q, context, True)
            return
        if data == "set_toggle_delold":
            new_val = "0" if db.get_setting("delete_previous_ad", "0") == "1" else "1"
            db.set_setting("delete_previous_ad", new_val)
            await ans(f"🧹 Xoá bài cũ: {'BẬT' if new_val == '1' else 'TẮT'}")
            await show_settings_menu(q, context, True)
            return

        # Groups
        if data.startswith("grp_toggle:"):
            cid = int(data.split(":")[1])
            res = db.toggle_group(cid)
            if res is False:
                await ans("⚠️ Chỉ được bật tối đa 3 nhóm!", show_alert=True)
            else:
                await ans("🟢 Đã bật nhóm!" if res else "⚪ Đã tắt nhóm!")
            await show_setup_screen(q, context, True)
            return
        if data.startswith("grp_del:"):
            db.remove_group(int(data.split(":")[1]))
            await ans("🗑 Đã xoá nhóm.")
            await show_setup_screen(q, context, True)
            return

        # Userbot
        if data == "ub_status":
            await ans()
            await show_userbot_status(q, context, True)
            return
        if data == "ub_qr_login":
            await ans("⏳ Đang tạo mã QR...")
            try:
                qr_login, png_bytes = await ub.create_qr_login()
            except Exception as e:
                await q.message.reply_text(f"❌ Không tạo được mã QR: {html.escape(str(e))}", parse_mode="HTML")
                return

            caption = (
                "📷 <b>QUÉT MÃ QR ĐỂ ĐĂNG NHẬP NICK CÁ NHÂN</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ Mở app <b>Telegram trên điện thoại</b>\n"
                "2️⃣ Vào <b>Cài đặt (Settings)</b> → <b>Thiết bị (Devices)</b>\n"
                "3️⃣ Bấm <b>Liên kết thiết bị máy tính (Link Desktop Device)</b>\n"
                "4️⃣ Đưa camera quét mã QR này (hiệu lực 2 phút)!\n\n"
                "<i>✨ Sau khi quét xong, Bot sẽ tự báo thành công ngay!</i>"
            )
            if png_bytes:
                qr_msg = await q.message.reply_photo(
                    photo=io.BytesIO(png_bytes),
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                qr_msg = await q.message.reply_text(
                    caption + f"\n\nLink đăng nhập: <code>{html.escape(qr_login.url)}</code>",
                    parse_mode="HTML",
                )

            chat_id = q.message.chat_id

            async def _wait_qr():
                try:
                    await qr_login.wait(timeout=120)
                    await ub._save_logged_in_info()
                    try:
                        await qr_msg.delete()
                    except Exception:
                        pass
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🎉 <b>ĐĂNG NHẬP USERBOT THÀNH CÔNG!</b>\n"
                            f"👤 Nick: <b>{html.escape(db.get_setting('userbot_name', ''))}</b>\n\n"
                            f"✅ Từ giờ Bot sẽ tự động dùng nick này đăng bài vào các nhóm — "
                            f"<b>KHÔNG CẦN thêm @bot vào nhóm nữa!</b>\n"
                            f"👉 Bạn có thể quay lại bài đăng bấm <b>🚀 Gửi thử ngay</b> hoặc vào <b>🔍 Dò danh sách nhóm</b>!"
                        ),
                        parse_mode="HTML",
                        reply_markup=get_menu_keyboard(),
                    )
                except asyncio.TimeoutError:
                    try:
                        await qr_msg.delete()
                    except Exception:
                        pass
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⌛ Mã QR đã hết hạn (quá 2 phút). Bấm <b>🔑 Userbot</b> → <b>📷 Quét mã QR</b> để lấy mã mới.",
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    if "SessionPasswordNeeded" in type(exc).__name__:
                        context.user_data["awaiting_input"] = {"mode": "ub_qr_2fa"}
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text="🔒 Đã quét QR thành công! Nick của bạn có bật mật khẩu 2 lớp (2FA).\n👉 Hãy gõ mật khẩu 2FA vào đây để hoàn tất:",
                            parse_mode="HTML",
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"❌ Lỗi đăng nhập QR: {html.escape(str(exc))}",
                            parse_mode="HTML",
                        )

            asyncio.create_task(_wait_qr())
            return

        if data == "ub_login_start":
            await ans()
            context.user_data["awaiting_input"] = {"mode": "ub_phone"}
            await q.message.reply_text(
                "📱 Nhập SĐT Telegram của bạn (ví dụ: <code>+84912345678</code> hoặc <code>0912345678</code>):",
                parse_mode="HTML",
            )
            return
        if data == "ub_logout":
            await ub.disconnect()
            await ans("✅ Đã đăng xuất nick!", show_alert=True)
            await show_userbot_status(q, context, True)
            return
        if data == "ub_scan":
            await ans("⏳ Đang quét danh sách nhóm trong nick...")
            await show_group_scan_results(q, context, 0, True)
            return
        if data.startswith("ub_scan_page:"):
            await ans()
            await show_group_scan_results(q, context, int(data.split(":")[1]), False)
            return
        if data.startswith("ub_grp_toggle:"):
            parts = data.split(":")
            cid, page = int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
            cache = ub._scan_cache
            grp = next((g for g in cache if g["chat_id"] == cid), None)
            if grp:
                existing = db.get_group(cid)
                if existing:
                    res = db.toggle_group(cid)
                    if res is False:
                        await ans("⚠️ Chỉ được bật tối đa 3 nhóm!", show_alert=True)
                    else:
                        await ans(f"{'🟢 BẬT' if res else '⚪ TẮT'}: {grp['title'][:30]}")
                else:
                    res = db.add_or_update_group(cid, grp["title"], grp.get("username"))
                    if not res["active"]:
                        await ans("⚠️ Đã đủ 3 nhóm đang bật!", show_alert=True)
                    else:
                        await ans(f"✅ Đã chọn nhóm: {grp['title'][:30]}")
                for g in ub._scan_cache:
                    if g["chat_id"] == cid:
                        db_g = db.get_group(cid)
                        g["in_db"] = db_g is not None
                        g["active"] = bool(db_g and db_g.get("active", 0)) if db_g else False
            await show_group_scan_results(q, context, page, False)
            return

        # Posts
        if data.startswith("post_view:"):
            await ans()
            pid = int(data.split(":")[1])
            await safe_edit_message(q, build_post_detail_text(pid), reply_markup=build_post_keyboard(pid))
            return
        if data.startswith("post_toggle:"):
            pid = int(data.split(":")[1])
            post = db.get_post(pid)
            if post:
                new_active = 0 if post["active"] else 1
                if new_active == 1:
                    prog = db.get_post_progress(pid)
                    max_r = int(post["max_runs"])
                    if max_r > 0 and prog and all(int(p["runs_done"]) >= max_r for p in prog):
                        db.reset_post_progress(pid)
                db.update_post(pid, active=new_active)
                await ans("▶️ Đã BẬT chạy tự động!" if new_active else "⏸ Đã TẠM DỪNG bài đăng!")
                await safe_edit_message(q, build_post_detail_text(pid), reply_markup=build_post_keyboard(pid))
            return
        if data.startswith("post_runs:"):
            _, ps, rs = data.split(":")
            pid, r_val = int(ps), int(rs)
            db.update_post(pid, max_runs=r_val)
            await ans(f"🎯 Đã đặt: {'Vô hạn' if r_val == 0 else f'{r_val} lần/nhóm'}")
            await safe_edit_message(q, build_post_detail_text(pid), reply_markup=build_post_keyboard(pid))
            return
        if data.startswith("post_int:"):
            _, ps, is_ = data.split(":")
            pid, i_val = int(ps), int(is_)
            db.update_post(pid, interval_minutes=i_val, fixed_times="")
            await ans(f"⏱ Đã đặt chu kỳ: mỗi {i_val} phút")
            await safe_edit_message(q, build_post_detail_text(pid), reply_markup=build_post_keyboard(pid))
            return
        if data.startswith("post_mode:"):
            pid = int(data.split(":")[1])
            post = db.get_post(pid)
            if post:
                new_m = "copy" if post["send_mode"] == "forward" else "forward"
                db.update_post(pid, send_mode=new_m)
                await ans(f"🔄 Đã đổi chế độ: {'Copy (ẩn nguồn)' if new_m == 'copy' else 'Forward'}")
                await safe_edit_message(q, build_post_detail_text(pid), reply_markup=build_post_keyboard(pid))
            return
        if data.startswith("post_ask_runs:"):
            await ans()
            pid = int(data.split(":")[1])
            context.user_data["awaiting_input"] = {"mode": "runs", "post_id": pid}
            await q.message.reply_text(f"✏️ Nhập số lần đăng cho Bài #{pid} (gõ 0 = vô hạn):")
            return
        if data.startswith("post_ask_int:"):
            await ans()
            pid = int(data.split(":")[1])
            context.user_data["awaiting_input"] = {"mode": "interval", "post_id": pid}
            await q.message.reply_text("✏️ Nhập số phút giữa mỗi lần đăng (ví dụ: 15):")
            return
        if data.startswith("post_ask_time:"):
            await ans()
            pid = int(data.split(":")[1])
            context.user_data["awaiting_input"] = {"mode": "fixed_time", "post_id": pid}
            await q.message.reply_text(
                "🕒 Nhập giờ cố định, cách nhau bởi dấu phẩy:\nVí dụ: <code>08:00, 12:30, 20:00</code>\n(gõ 'xoa' để xoá giờ cố định)",
                parse_mode="HTML",
            )
            return
        if data.startswith("post_preview:"):
            await ans("👁 Đang gửi bản xem trước...")
            pid = int(data.split(":")[1])
            post = db.get_post(pid)
            if post:
                try:
                    await context.bot.copy_message(q.message.chat_id, post["storage_chat_id"], post["storage_message_id"])
                except Exception:
                    try:
                        await context.bot.forward_message(q.message.chat_id, post["storage_chat_id"], post["storage_message_id"])
                    except Exception as e2:
                        await q.message.reply_text(f"❌ Không xem được: {e2}")
            return
        if data.startswith("post_sendnow:"):
            pid = int(data.split(":")[1])
            post = db.get_post(pid)
            groups = db.get_groups(active_only=True)
            if not post or not groups:
                await ans("⚠️ Chưa có bài hoặc chưa bật nhóm nào!", show_alert=True)
                return
            db.ensure_progress_rows(pid)
            prog = db.get_post_progress(pid)
            prog.sort(key=lambda p: (p["runs_done"], p.get("last_sent_at") or ""))
            tg = db.get_group(prog[0]["group_id"])
            ok, detail = await scheduler.send_single_post_to_group(context.bot, post, tg, datetime.now())
            if ok:
                await ans(f"🚀 Đã gửi thành công vào:\n{tg['title'][:40]}!", show_alert=True)
            else:
                await ans(f"❌ Chưa gửi được:\n{detail[:140]}", show_alert=True)
            await safe_edit_message(q, build_post_detail_text(pid), reply_markup=build_post_keyboard(pid))
            return
        if data.startswith("post_reset:"):
            pid = int(data.split(":")[1])
            db.reset_post_progress(pid)
            await ans("♻️ Đã reset số lần đăng về 0!", show_alert=True)
            await safe_edit_message(q, build_post_detail_text(pid), reply_markup=build_post_keyboard(pid))
            return
        if data.startswith("post_del:"):
            pid = int(data.split(":")[1])
            db.delete_post(pid)
            await ans(f"🗑 Đã xoá Bài #{pid}!")
            await show_posts_list(q, context, True)
            return
    finally:
        await ans()


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        print("=" * 65)
        token = input("Dán BOT_TOKEN từ @BotFather vào đây: ").strip()
        if not token:
            print("❌ Chưa nhập Token. Thoát..."); return
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        content = open(env_path, encoding="utf-8").read()
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(content.replace("YOUR_BOT_TOKEN_HERE", token))
        print("✅ Đã lưu BOT_TOKEN vào .env!")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addgroup", addgroup_cmd))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_members))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    app.job_queue.run_repeating(scheduler.tick, interval=20, first=5)

    async def on_startup(app):
        ub = get_manager(db)
        if await ub.start_silent():
            logger.info(f"[Userbot] Kết nối tự động: {db.get_setting('userbot_name')}")

    app.post_init = on_startup

    print("🚀 BOT ĐANG CHẠY! Mở Telegram → nhắn /start cho Bot.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
