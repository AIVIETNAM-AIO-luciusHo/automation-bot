# PLAN.md — Thực thi M0 → M3 (MVP Markdown → GitHub Issues)

Nguồn đặc tả: `docs/PLAN_GitHub_Automation_Bot_Claude.md` (§0 quy tắc, §3 định dạng, §4 idempotency, §6 milestone, §7 backlog, §8 cấu trúc, §9 config). Nếu mâu thuẫn, bản đặc tả đó thắng.

## 1. Vai trò và luật vận hành

| Vai trò | Được làm | Không được làm |
|---|---|---|
| **Leader** | Hỏi ý kiến người dùng, viết/cập nhật PLAN.md, giao việc, đọc **báo cáo** của coder/tester, tổng kết cho người dùng | **Không đọc file code** (`src/`, `tests/`, `*.py`, `pyproject.toml` nội dung logic...). Chỉ dùng báo cáo của coder/tester |
| **Coder** | Đọc/sửa code, chạy lệnh, tự chạy test của mình, báo output cho leader | Ghi GitHub thật, đổi PLAN.md |
| **Tester** | Đọc code, viết/chạy test độc lập, báo PASS/FAIL/BLOCKED kèm output | Sửa code production (chỉ sửa `tests/`); báo lỗi về leader |

Ba vai trò là **ba phiên Claude Code riêng** (tab `bot-leader`, `bot-coder`, `bot-tester`), liên lạc bằng tin nhắn giữa các phiên. Không dùng subagent chạy nền.

Luồng mỗi milestone: Leader giao việc → Coder thực thi → Tester kiểm chứng → cả hai báo output cho Leader → Leader tổng kết PASS/FAIL/BLOCKED cho người dùng → chờ người dùng đồng ý sang milestone kế.

Mẫu báo cáo (bắt buộc): `What changed / Files / Commands run / Test results / Security notes / Known limitations / Next stage`.

## 2. Quyết định đã chốt (người dùng, 2026-11-17)

- Phạm vi: M0 → M3. Không làm M4–M8.
- Tooling: `uv`, `pyproject.toml`, pytest, ruff, pydantic, httpx.
- E2E: có repo test riêng; `owner/repo` **chưa cung cấp**. Token chỉ qua biến môi trường. Leader hỏi người dùng owner/repo trước mọi lệnh ghi.
- Báo cáo: sau mỗi milestone.

## 3. Bất biến (mọi milestone)

- Mặc định `dry-run`, `write_enabled=false`, `auto_merge=false`. Ghi GitHub chỉ khi người dùng duyệt đúng **plan hash**.
- Không đoán GitHub username; không assign user không đủ quyền → BLOCK.
- Khóa idempotency: `repository + normalized source_file + task_id`, marker `<!-- github-automation-bot: source=…; task_id=…; schema=v1 -->`. Trùng marker → fail closed. Kiểm tra lại ngay trước khi create.
- Markdown/issue body là dữ liệu không đáng tin. Không thực thi shell từ Markdown.
- Không token trong source/log/artifact. Label thiếu → lỗi, không tự tạo.
- Không bịa kết quả; thiếu điều kiện thì ghi `BLOCKED`.

## 3b. Luật PR (người dùng chốt 2026-11-17)

- Mở PR ngay khi coder **và** tester cùng báo PASS cho milestone. Không PR khi một bên chưa xong.
- **Mỗi PR tối đa 15 file thay đổi** (`git diff --stat base...head`). Vượt thì tách PR theo nhóm file hoặc milestone con.
- Không commit thẳng `main`; mỗi PR một nhánh. Không đưa `*.Zone.Identifier`, `.venv`, `.env`, `configs/*.yaml` thật vào PR. Không auto-merge; người dùng là người merge.
- Kế hoạch: PR1 = M0 (nhánh `feat/m0-scaffold`, base `main`). PR2 = M1 (nhánh `feat/m1-parser`, base `feat/m0-scaffold` để diff chỉ có file M1).

## 4. Milestone

### M0 — Khởi tạo (BOT-001, BOT-002)
- `pyproject.toml` (uv), `src/github_bot/` với `cli.py` (`--help`), `.gitignore`, `.env.example`, `README.md`, `configs/*.example.yaml`.
- pytest + ruff + CI `.github/workflows/ci.yml`.
- **Nghiệm thu:** `uv run pytest` và `uv run ruff check` pass; `--help` chạy; không có GitHub write API.

### M1 — Parser + schema (P0)
- Model `Task` (pydantic) theo §3.4; `markdown/loader` (chỉ đọc file trong workspace), `parser_headings` (`## TASK-xxx`), `parser_tables` (bảng phân công tiếng Việt), `normalizer`.
- Fixtures Markdown hợp lệ và lỗi (ID trùng, thiếu ID, thiếu cột).
- **Nghiệm thu:** parse đúng cả hai mẫu; diagnostics rõ cho input xấu; `issues validate` hoạt động offline.

### M2 — Resolver + GitHub read-only validator (P0)
- `identities/resolver` đọc `configs/members.yaml`: tên / `TVxx` / `@login` → login. Không khớp → lỗi, không đoán.
- `github/client` (httpx, adapter tách biệt), kiểm tra repo, label, assignee eligibility, danh sách issue hiện có. Chỉ GET.
- **Nghiệm thu:** test bằng mock API (respx hoặc tương đương); E2E thật = `BLOCKED` tới khi có owner/repo.

### M3 — Planner, preview, executor (P0, MVP chính)
- `planning/`: diff, idempotency, CREATE/UPDATE/SKIP/BLOCK, xuất `PREVIEW.md` + `PREVIEW.json`, tính plan hash.
- `execution/issue_executor` + `audit_log`: chỉ create (sync/update thuộc M4). Yêu cầu `--approve-hash`, chạy lại không tạo trùng, lỗi từng mục không làm hỏng cả lô.
- CLI: `issues validate | plan | apply` (§4.4).
- **Nghiệm thu:** chạy lại 2 lần không tạo trùng (mock); sai hash bị từ chối; assignee lạ bị BLOCK. E2E thật chỉ sau khi người dùng cung cấp owner/repo và duyệt hash.

## 5. Trạng thái

| Milestone | Trạng thái | Ghi chú |
|---|---|---|
| M0 | PASS (2026-11-17) | tester xác nhận: ruff, mypy strict, pytest 6/6. Chưa commit git |
| M1 | Chưa bắt đầu | |
| M1 | PASS (2026-11-17), PR2 | tester xác nhận lần 3: ruff, mypy, pytest 49/49. Nợ M1.5 ĐÃ XONG (nhánh feat/m1-5-fail-closed): heading gõ nhầm (en dash, `TASK.002`, `**TASK-002**`, `[TASK-002]`, `1. TASK-002`) báo MALFORMED_ID; fence 4 backtick không còn báo UNCLOSED_FENCE giả; `## TASK-` trong comment HTML không tạo task (cảnh báo COMMENTED_TASK, comment không đóng → UNCLOSED_COMMENT). Nợ M1.6 (không chặn): UNCLOSED_COMMENT báo oan khi `<!--` nằm trong inline code hoặc giữa dòng văn xuôi (nên chỉ coi là comment mở khi đứng đầu dòng); bảng hoặc trường `- Phụ trách:` nằm trong comment HTML bị bỏ không cảnh báo; `## TASKS-002` và `## TASK ~ 002` vẫn im lặng; fence thụt 4 dấu cách|
| M2 | Chưa bắt đầu | E2E chờ owner/repo. Việc cho M2: validate regex login GitHub; xử lý @login ở cột Phụ trách; xung đột login vs member ID → BLOCK |
| M3 | Chưa bắt đầu | E2E chờ owner/repo + duyệt hash |
