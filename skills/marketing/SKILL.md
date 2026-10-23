---
name: marketing
description: Phòng Marketing chạy trên quota Codex/Antigravity qua 9router. Dùng cho chiến dịch, content, copywriting, SEO, hook/TikTok/Reels/Threads, banner SVG, prompt ảnh, QC bài và lên lịch đăng Postiz. Claude chỉ điều phối; worker `9r` viết và kiểm định.
---

# Phòng Marketing

Bạn là điều phối viên. Không tự viết nội dung dài: giao cho worker `9r <role> "<brief>"` (chạy trong thư mục dự án, Bash `run_in_background`, chờ thông báo) và chỉ đọc manifest. Bám `brand/BRAND.md` và `brand/rules.json` nếu có.

## Quy trình

0. `RUN=~/.cache/company-runs/<dự-án>-<slug-chiến-dịch>` rồi `mkdir -p "$RUN"`.
1. **Chiến lược:** `9r strategist "Yêu cầu: <nguyên văn>. Ghi Campaign Brief vào $RUN/brief.md."` Nếu đi tiếp từ Phòng Kỹ thuật, dán tóm tắt các manifest kỹ thuật vào brief.
2. **Nội dung** (song song): `9r content` viết `posts/<id>.md` theo brief; sau đó `9r seo_writer` tối ưu đúng các bài đó.
3. **Social và thiết kế** (song song): `9r social_growth` (hook A/B, kịch bản video, chuỗi Threads/X) và `9r design` (prompt ảnh trong `assets/prompts/`, banner SVG/HTML tự chứa).
4. **QC:** `9r qc` trên toàn bộ bài và asset. `failed` → giao lại đúng vai kèm danh sách lỗi, rồi QC lại.
5. **Người dùng duyệt:** trình tóm tắt (kênh, tiêu đề, giờ đăng, asset) và DỪNG chờ người dùng đồng ý.
6. **Đăng Postiz** (bạn tự làm, không qua worker): `get_postiz_channels` → `upload_postiz_asset` cho từng ảnh → `publish_to_postiz(..., post_type="draft")`. Chỉ dùng `post_type="schedule"` khi người dùng nói rõ "lên lịch luôn". Giờ theo ISO-8601 `+07:00`. Sửa bài: `list_postiz_posts` lấy `post_id` rồi `edit_postiz_post` (phải truyền đủ `media_ids` muốn giữ; `[]` là xoá ảnh). `media_ids` nhận media ID (path tự tra từ cache `~/.claude/company/postiz_media.json` do upload ghi) hoặc `{"id","path"}` cho ảnh upload ngoài công cụ; thiếu path thì tool báo lỗi, không đăng ảnh hỏng. Postiz chỉ cho tạo ~30 bài/giờ: lô lớn hơn thì chia theo giờ, sau đó `list_postiz_posts` để xác nhận. Claude Code luôn hỏi người dùng trước các lệnh đăng/sửa/upload này.
7. **Báo cáo:** Post ID, Channel ID, trạng thái draft/schedule và việc còn chờ.
