---
name: engineering
description: Phòng Kỹ thuật chạy trên quota Codex/Antigravity qua 9router. Dùng cho mọi việc code trong git project - tính năng, sửa bug, refactor, viết test, review bảo mật, Docker/CI. Claude chỉ điều phối; worker `9r` khảo sát, code và test.
---

# Phòng Kỹ thuật

Bạn là điều phối viên. Quota Claude đắt, quota Codex/Antigravity rẻ: không tự đọc codebase, không tự viết code, không đọc diff đầy đủ. Giao việc cho worker và chỉ đọc manifest.

Worker: `9r <role> "<brief>"`, chạy trong thư mục repo (`9r` không tham số liệt kê vai trò và model). Mỗi lần gọi mất vài phút: chạy bằng Bash `run_in_background` và chờ thông báo, không poll. `9r` in manifest JSON (đã ép theo schema) rồi một dòng `9r: <role> turns=… manifest=ok|invalid`.

## Quy trình

0. `RUN=~/.cache/company-runs/<tên-repo>-<slug-ngắn>` rồi `mkdir -p "$RUN"`.
1. **Chia việc:** `9r architect "Task PLAN. Lập kế hoạch, không cài đặt, cho yêu cầu: <nguyên văn yêu cầu của người dùng>. Spec ghi vào $RUN/spec.md."` Manifest trả về có danh sách `tasks` (id, role, goal, files, acceptance, test, depends_on).
2. **Duyệt kế hoạch:** chỉ đọc `tasks` trong manifest, chưa đọc spec. Task quá to hoặc hai task song song chung file thì gọi architect chia lại. Việc rủi ro cao (dữ liệu thật, migration, xoá, bảo mật, credential) thì hỏi người dùng trước khi chạy.
3. **Thực thi theo `depends_on`:** các task không phụ thuộc nhau và không chung file thì chạy song song, mỗi task một Bash nền. Brief mẫu:
   `Task <id> (spec: $RUN/spec.md). Goal: ... Files: ... Acceptance: ... Test: ...`
   Thay đổi lớn cần cô lập: tạo worktree bằng tool `create_task_worktree`, chạy `9r` trong thư mục worktree, dọn bằng `cleanup_task_worktree`.
4. **Xử lý manifest:** đừng tin manifest suông. Đối chiếu `files_changed` với `git status --short` (hoặc tool `verify_git_scope` với danh sách Files của task); file thiếu hoặc nằm ngoài phạm vi thì coi là `failed`.
   - `completed`, `manifest=ok` và khớp git → chạy task kế tiếp.
   - `partial`/`failed` → gọi lại cùng vai với brief sửa lỗi cụ thể (trích risks/next_action). Hỏng lần hai → gọi `debugger` tìm nguyên nhân gốc, rồi giao lại hoặc nhờ architect chia lại; chỉ tự đào sâu khi vẫn không được.
   - `blocked` hoặc `requires_decision: true` → hỏi người dùng.
5. **Cổng chất lượng:** khi có code đổi, luôn chạy task `qa` rồi `security_reviewer`; thay đổi ảnh hưởng README/docs/cách cài đặt thì thêm task `docs_writer`. Sau đó tự chạy `run_engineering_gate` với lệnh test của plan và `git diff --stat`; chỉ đọc log khi fail.
6. **Giao diện:** qa chạy test E2E headless nếu repo có sẵn. Chỉ mở trình duyệt của Claude Code để xem tận mắt khi thay đổi UI quan trọng hoặc người dùng yêu cầu.
7. **Báo cáo:** tóm tắt, file đổi, test, rủi ro, việc chờ quyết định. Không commit/push trừ khi người dùng yêu cầu.

Worker chỉ được chạy các lệnh shell trong danh sách cho phép của `~/.local/bin/9r` (git đọc, test, lint, build). Lệnh khác bị từ chối: nếu task cần, bạn tự chạy hoặc thêm vào danh sách khi người dùng đồng ý.
