import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Set
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from database import Database

logger = logging.getLogger(__name__)


class SmartScheduler:
    """
    Bộ điều phối xoay vòng chống trùng bài đăng cho 3 nhóm Telegram:
    1. Không gửi trùng cùng 1 bài vào 3 nhóm tại cùng một thời điểm (mỗi nhóm nhận 1 bài khác nhau).
    2. Trong cùng 1 nhóm, không lặp lại bài vừa đăng trước đó (xoay vòng xen kẽ Bài 1 -> Bài 2 -> Bài 3).
    3. Nếu chỉ có 1 bài duy nhất đang chạy, tự động rải lệch nhịp giữa các nhóm để không bị trùng giờ đăng.
    4. Đếm chính xác số lần đã đăng của từng bài ở từng nhóm, tự động dừng khi đạt đủ số lần cài đặt.
    """

    def __init__(self, db: Database):
        self.db = db
        self._lock = asyncio.Lock()

    def _is_post_due_for_group(
        self,
        post: Dict[str, Any],
        progress: Dict[str, Any],
        now: datetime,
    ) -> bool:
        # Kiểm tra đã đủ số lần đăng chưa (max_runs > 0 nghĩa là có giới hạn)
        max_runs = int(post.get("max_runs", 0))
        runs_done = int(progress.get("runs_done", 0))
        if max_runs > 0 and runs_done >= max_runs:
            return False

        # Kiểm tra khung giờ cố định (nếu người dùng cài đặt giờ cụ thể dạng "08:00,12:00,20:00")
        fixed_times_str = (post.get("fixed_times") or "").strip()
        if fixed_times_str:
            current_hm = now.strftime("%H:%M")
            target_times = [t.strip() for t in fixed_times_str.split(",") if t.strip()]
            if current_hm not in target_times:
                # Cho phép trễ tối đa 3 phút trong khung giờ nếu nhóm vừa bận bài khác
                matched_window = False
                for t_str in target_times:
                    try:
                        t_obj = datetime.strptime(t_str, "%H:%M").replace(
                            year=now.year, month=now.month, day=now.day
                        )
                        diff_sec = (now - t_obj).total_seconds()
                        if 0 <= diff_sec <= 180:
                            matched_window = True
                            break
                    except ValueError:
                        continue
                if not matched_window:
                    return False

            # Đảm bảo không gửi 2 lần trong cùng 1 khung giờ
            last_sent_str = progress.get("last_sent_at")
            if last_sent_str:
                try:
                    last_sent = datetime.fromisoformat(last_sent_str)
                    if (now - last_sent).total_seconds() < 300:
                        return False
                except ValueError:
                    pass
            return True

        # Kiểm tra theo chu kỳ khoảng cách phút (interval_minutes)
        next_avail_str = progress.get("next_available_at")
        if next_avail_str:
            try:
                next_avail = datetime.fromisoformat(next_avail_str)
                if now < next_avail:
                    return False
            except ValueError:
                pass

        return True

    def _is_group_ready(self, group: Dict[str, Any], now: datetime) -> bool:
        """Kiểm tra nhóm đã nghỉ đủ thời gian giãn cách tối thiểu giữa 2 bài bất kỳ chưa."""
        min_cooldown = int(self.db.get_setting("min_group_cooldown", "2"))
        last_sent_str = group.get("last_sent_at")
        if not last_sent_str or min_cooldown <= 0:
            return True
        try:
            last_sent = datetime.fromisoformat(last_sent_str)
            return (now - last_sent) >= timedelta(minutes=min_cooldown)
        except ValueError:
            return True

    def _is_post_staggered_across_groups(self, post_id: int, now: datetime) -> bool:
        """
        Đảm bảo CÙNG MỘT BÀI VIẾT không bị gửi vào 2 nhóm khác nhau quá sát giờ nhau
        (mặc định cách nhau ít nhất stagger_minutes phút nếu thiếu bài để xoay vòng).
        """
        stagger_mins = int(self.db.get_setting("stagger_minutes", "3"))
        if stagger_mins <= 0:
            return True
        progress_list = self.db.get_post_progress(post_id)
        for p in progress_list:
            last_sent_str = p.get("last_sent_at")
            if last_sent_str:
                try:
                    last_sent = datetime.fromisoformat(last_sent_str)
                    if (now - last_sent) < timedelta(minutes=stagger_mins):
                        return False
                except ValueError:
                    continue
        return True

    async def send_single_post_to_group(
        self,
        bot,
        post: Dict[str, Any],
        group: Dict[str, Any],
        now: datetime,
    ) -> bool:
        """Thực hiện chuyển tiếp (Forward) hoặc sao chép (Copy) bài viết vào 1 nhóm."""
        post_id = post["id"]
        group_id = group["chat_id"]
        progress = self.db.get_progress_entry(post_id, group_id)

        # Tuỳ chọn tự xoá bài quảng cáo cũ trong nhóm trước khi đăng bài mới
        if self.db.get_setting("delete_previous_ad", "0") == "1":
            old_msg_id = progress.get("last_message_id")
            if old_msg_id:
                try:
                    await bot.delete_message(chat_id=group_id, message_id=int(old_msg_id))
                except Exception:
                    pass

        try:
            if post.get("send_mode", "forward") == "forward":
                sent_msg = await bot.forward_message(
                    chat_id=group_id,
                    from_chat_id=post["storage_chat_id"],
                    message_id=post["storage_message_id"],
                )
            else:
                sent_msg = await bot.copy_message(
                    chat_id=group_id,
                    from_chat_id=post["storage_chat_id"],
                    message_id=post["storage_message_id"],
                )

            interval_mins = max(1, int(post.get("interval_minutes", 30)))
            next_avail = (now + timedelta(minutes=interval_mins)).isoformat(timespec="seconds")
            now_str = now.isoformat(timespec="seconds")

            self.db.record_post_sent(
                post_id=post_id,
                group_id=group_id,
                sent_message_id=sent_msg.message_id,
                sent_at=now_str,
                next_available_at=next_avail,
            )
            self.db.add_log(
                post_id=post_id,
                group_id=group_id,
                group_title=group["title"],
                post_title=post["title"],
                status="SUCCESS",
                detail=f"Lần {progress.get('runs_done', 0) + 1}/{post['max_runs'] if post['max_runs'] > 0 else '∞'}",
            )
            return True

        except TelegramError as e:
            logger.error(f"Lỗi khi gửi bài #{post_id} vào nhóm {group['title']} ({group_id}): {e}")
            self.db.add_log(
                post_id=post_id,
                group_id=group_id,
                group_title=group["title"],
                post_title=post["title"],
                status="ERROR",
                detail=str(e)[:120],
            )
            return False

    async def check_and_complete_posts(self, bot, active_posts: List[Dict[str, Any]], active_groups: List[Dict[str, Any]]):
        """Tự động tắt bài viết khi đã đăng đủ số lần ở tất cả các nhóm đang bật và thông báo cho Admin."""
        if not active_groups:
            return

        admin_chat_id = self.db.get_setting("admin_chat_id", "")
        notify = self.db.get_setting("notify_on_complete", "1") == "1"

        for post in active_posts:
            max_runs = int(post.get("max_runs", 0))
            if max_runs <= 0:
                continue  # Chế độ vô hạn

            progress_list = self.db.get_post_progress(post["id"])
            if not progress_list:
                continue

            # Kiểm tra tất cả các nhóm đang active đã đạt >= max_runs chưa
            all_completed = all(int(p["runs_done"]) >= max_runs for p in progress_list)
            if all_completed:
                self.db.update_post(post["id"], active=0)
                if notify and admin_chat_id:
                    try:
                        summary_lines = [
                            f"🎉 <b>HOÀN THÀNH LỊCH ĐĂNG BÀI #{post['id']}</b>",
                            f"📌 Bài viết: <b>{post['title']}</b>",
                            "━━━━━━━━━━━━━━━━━━",
                        ]
                        for p in progress_list:
                            summary_lines.append(
                                f"✅ <b>{p['group_title']}</b>: Đã đăng đủ {p['runs_done']}/{max_runs} lần"
                            )
                        summary_lines.append("\nBài viết đã tự động chuyển sang trạng thái <b>Đã hoàn thành</b>.")
                        await bot.send_message(
                            chat_id=int(admin_chat_id),
                            text="\n".join(summary_lines),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"Không thể gửi thông báo hoàn thành cho admin: {e}")

    async def tick(self, context: ContextTypes.DEFAULT_TYPE):
        """Hàm chạy định kỳ mỗi 30 giây để phân phối bài viết không trùng nhau vào 3 nhóm."""
        if self._lock.locked():
            return

        async with self._lock:
            now = datetime.now()
            active_groups = self.db.get_groups(active_only=True)
            active_posts = self.db.get_posts(active_only=True)

            if not active_groups or not active_posts:
                return

            # Đảm bảo bảng tiến độ có đủ các cặp (post_id, group_id)
            for post in active_posts:
                self.db.ensure_progress_rows(post["id"])

            # Tập hợp các bài đã được chọn trong lượt quét hiện tại
            # -> Đảm bảo trong cùng 1 đợt, 3 nhóm KHÔNG BAO GIỜ nhận cùng 1 bài trùng nhau!
            used_post_ids_this_tick: Set[int] = set()

            for group_idx, group in enumerate(active_groups):
                if not self._is_group_ready(group, now):
                    continue

                group_id = group["chat_id"]
                last_post_id_in_group = group.get("last_post_id")

                # Tìm danh sách ứng viên đủ điều kiện đăng vào nhóm này
                candidates: List[Dict[str, Any]] = []
                for post in active_posts:
                    pid = post["id"]
                    # Luật 1: Không chọn bài vừa được phân cho nhóm khác trong cùng lượt này
                    # hoặc chưa đủ độ lệch thời gian giữa các nhóm
                    if pid in used_post_ids_this_tick:
                        continue
                    if not self._is_post_staggered_across_groups(pid, now):
                        continue

                    progress = self.db.get_progress_entry(pid, group_id)
                    if self._is_post_due_for_group(post, progress, now):
                        candidates.append({
                            "post": post,
                            "progress": progress,
                            "is_repeat_in_group": (pid == last_post_id_in_group),
                            "runs_done": int(progress.get("runs_done", 0)),
                            "last_sent_at": progress.get("last_sent_at") or "",
                            # Tạo độ lệch xoay vòng tự nhiên theo chỉ số nhóm (group_idx)
                            "rotation_score": (pid + group_idx) % max(1, len(active_posts)),
                        })

                if not candidates:
                    continue

                # Luật 2: Ưu tiên tuyệt đối bài KHÁC với bài vừa đăng gần nhất ở nhóm đó (is_repeat_in_group = False)
                # Tiếp theo ưu tiên bài có số lần đăng ở nhóm đó ít hơn (runs_done nhỏ hơn),
                # Tiếp theo là bài lâu chưa được đăng nhất ở nhóm đó (last_sent_at cũ nhất),
                # Cuối cùng là điểm xoay vòng lệch pha giữa 3 nhóm (rotation_score)
                candidates.sort(
                    key=lambda c: (
                        1 if c["is_repeat_in_group"] else 0,
                        c["runs_done"],
                        c["last_sent_at"],
                        c["rotation_score"],
                    )
                )

                chosen = candidates[0]["post"]
                used_post_ids_this_tick.add(chosen["id"])

                await self.send_single_post_to_group(
                    bot=context.bot,
                    post=chosen,
                    group=group,
                    now=now,
                )
                # Nghỉ 1.5s giữa các lệnh gọi API để an toàn tuyệt đối với giới hạn của Telegram
                await asyncio.sleep(1.5)

            # Kiểm tra nếu có bài nào đã hoàn thành đủ số lần ở cả 3 nhóm
            await self.check_and_complete_posts(context.bot, active_posts, active_groups)
