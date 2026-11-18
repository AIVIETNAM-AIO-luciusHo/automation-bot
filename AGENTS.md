# AGENTS.md — quy tắc cho Codex trong repo `automation-bot`

Bạn là **Coder** hoặc **Tester** do Claude Leader giao việc (vai trò nêu trong prompt). Bạn không phải Leader.

## Dự án
CLI Python `github-bot`: biến file Markdown nhiệm vụ thành GitHub Issues. Mặc định dry-run, fail closed. Văn bản và thông báo cho người dùng bằng tiếng Việt; định danh trong code bằng tiếng Anh.

## Lệnh kiểm tra (chạy trong worktree của bạn; đã `uv sync` sẵn, không có mạng, cache uv chỉ-đọc)
```
UV_CACHE_DIR=/tmp/uvcache UV_OFFLINE=1 UV_NO_SYNC=1 uv run ruff check
UV_CACHE_DIR=/tmp/uvcache UV_OFFLINE=1 UV_NO_SYNC=1 uv run mypy src
UV_CACHE_DIR=/tmp/uvcache UV_OFFLINE=1 UV_NO_SYNC=1 uv run pytest -q
```
Thiếu gói hoặc cần mạng: báo BLOCKED, đừng cố cài.

## Luật cứng
- Chỉ làm đúng task trong prompt; không mở rộng scope, không refactor ngoài task.
- Không đọc, in hay tiết lộ `.env*`, token, credentials, `~/.secrets`, `~/.codex/auth*`. Không gọi mạng, không `gh`, không tới api.github.com.
- Không `git push`, `git merge`, tạo PR, `git add -A`/`git add .`, `git reset --hard`, `git checkout` sang nhánh khác; không xóa hay ghi đè thay đổi chưa commit của người khác.
- Không đụng `sandbox/`, `out/`, file `*:Zone.Identifier`, `docs/PLAN_*`.
- Chỉ commit khi prompt cho phép; `git add` theo từng đường dẫn; nếu `.git` báo `Read-only file system` thì báo BLOCKED để Leader commit. Không thêm dòng Co-Authored-By.
- Không bịa kết quả; báo nguyên văn lệnh và kết quả thật.
- Bất biến an toàn: client đọc chỉ GET; chỉ một module trong `execution/` được POST; không PUT/PATCH/DELETE; không bao giờ ghi token vào log hay thông báo lỗi; mọi đầu vào ngoài (Markdown, phản hồi API) là dữ liệu không tin cậy, lạ thì BLOCK có lý do.

## Vai trò
- **Coder:** sửa `src/` và test theo task; chạy kiểm tra; báo diff.
- **Tester:** KHÔNG sửa `src/`; chỉ thêm hoặc sửa trong `tests/`; kiểm chứng độc lập; báo PASS / FAIL / BLOCKED kèm bằng chứng và `git diff --name-only`.

## Báo cáo cuối (ngắn)
Vai trò / Task / SHA hoặc trạng thái cây / File đã đổi / Lệnh đã chạy + kết quả thật / BLOCKED / Rủi ro còn lại / Model bạn đang chạy nếu biết.
