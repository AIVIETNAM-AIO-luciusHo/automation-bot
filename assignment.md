# assignment.md — Quy trình Claude Leader + Codex Coder + Codex Tester (một phiên duy nhất)

> **Cách dùng ở project mới:** copy file này vào gốc repo, mở **một** phiên Claude Code ở đó và nói:
> `Đọc assignment.md rồi vận hành theo đó. Bắt đầu bằng Mục 2 (khởi động).`
> Phiên đó là **Leader**. Coder và Tester là các phiên **Codex** do Leader gọi bằng `codex-plugin-cc`. Không cần mở thêm tab nào.
>
> Tài liệu này được viết từ pilot thật ở repo `automation-bot` (2026-11-18). Mục nào ghi **[đã kiểm chứng]** thì có bằng chứng; **[chưa kiểm chứng]** thì phải thử lại ở project mới trước khi tin.

---

## 0. Mô hình

| Vai trò | Công cụ | Làm gì | Không làm |
|---|---|---|---|
| **Owner** (bạn) | | Duyệt kế hoạch, duyệt push/PR, merge, mọi lệnh ghi dịch vụ ngoài | |
| **Leader** | Claude Code | Phân tích, kế hoạch, tiêu chí nghiệm thu, giao việc, đọc báo cáo, kiểm Git metadata, commit/push/PR khi được phép, trình Owner | **Không đọc source code của dự án**, không viết production code |
| **Coder** | Codex (`task --write`) | Đọc và sửa code, chạy ruff/mypy/pytest, báo diff | Push, PR, merge, mở rộng scope |
| **Tester** | Codex (phiên `--fresh`, worktree riêng) | Viết test trước, kiểm chứng độc lập trên đúng SHA | Sửa production code, commit, push |

Leader chỉ được nhìn: tài liệu, báo cáo của Codex, `git status/diff --name-only/--stat/log/rev-parse`, `gh` chỉ-đọc, kết quả công cụ. Cần biết nội dung code thì **giao Codex đọc rồi báo bằng chứng**.

## 1. Luật cứng (mọi vai trò)
- Không đọc/in/tiết lộ `.env*`, token, credentials, `~/.secrets`, `~/.codex/auth*`. Không bao giờ để token vào chat, log, commit, thông báo lỗi.
- Không commit thẳng `main`. Không force-push, không merge, không `git add -A` / `git add .`, không `git reset --hard`, không xóa hay ghi đè thay đổi chưa commit.
- Không gọi API ghi của dịch vụ thật (GitHub...) khi Owner chưa cho phép rõ cho đúng lần đó. Giữ dry-run và approval-hash nếu dự án có.
- Không bịa kết quả test, trạng thái CI, tên model, số token. Chỉ nhận **PASS** khi có lệnh và kết quả thật.
- Không ghi thông tin tác giả hay công cụ sai thực tế (không gắn `Co-Authored-By: Claude` cho code do Codex viết).
- Mỗi PR là **một task cụ thể**, tối đa **15 file**. Push và mở PR chỉ sau khi **Coder xong và Tester PASS trên đúng SHA** sắp push, và Owner đã cho phép theo quy tắc đang áp dụng. Commit đổi sau khi Tester PASS thì phần đổi phải được kiểm lại.
- Sau **2 vòng sửa lỗi** Coder ↔ Tester không tiến triển: báo **BLOCKED** cho Owner, không lặp vô hạn.
- Review Gate của plugin **tắt** (mặc định).

## 2. Khởi động (Leader làm khi bắt đầu project mới)
Chỉ đọc, chưa sửa gì. Báo kết quả rồi chờ Owner duyệt.
1. Đọc `CLAUDE.md`, `PLAN.md`, `AGENTS.md`, README nếu có. `git status`, `git branch -a`, `git worktree list`, `gh pr list`, CI gần nhất.
2. Tìm companion: `find ~/.claude/plugins -name codex-companion.mjs` (gọi là `$CC` bên dưới).
3. `node $CC setup --json --cwd <repo>` → cần `ready: true`, `auth.loggedIn: true`. Chưa đăng nhập thì Owner tự chạy `!codex login`. Hỏi Owner xác nhận **đúng tài khoản ChatGPT** (không in email ra chat). `reviewGateEnabled` phải `false`.
4. Có `AGENTS.md` ở gốc chưa? Chưa có thì tạo từ **Phụ lục A**, điền lệnh kiểm tra của dự án. Codex đọc `AGENTS.md`, không đọc `CLAUDE.md` [thông lệ Codex CLI; prompt luôn nhắc nó nên chưa tách được hiệu quả riêng].
5. Điền **Mục 9 (Hồ sơ dự án)**: ngôn ngữ, lệnh lint/type/test, quy ước commit/PR, nhánh mặc định, thư mục cấm đụng.
6. Báo Owner: hiện trạng, việc dở, rủi ro, kế hoạch. Chỉ làm tiếp khi được duyệt.

## 3. Vòng đời một task
1. **Leader** viết task + hợp đồng kỹ thuật + tiêu chí PASS/FAIL (một đoạn ngắn, kiểm được bằng test).
2. **Tester (Codex)** viết/duyệt test trước, trong `tests/`; test chưa đạt đánh dấu `xfail(strict=True)` để cây vẫn xanh. Leader kiểm `git diff --name-only` ⊆ `tests/`.
3. **Coder (Codex)** nhận test (Leader `cp` đúng các file test sang worktree Coder), sửa code, gỡ đúng các mark xfail liên quan, chạy ruff/mypy/pytest.
4. **Leader commit cục bộ** (xem 5.4: Codex không commit được trong worktree). `git add` theo từng đường dẫn, không `-A`.
5. **Tester độc lập** kiểm trên **worktree mới, detached tại đúng SHA** (`--fresh`, không biết phiên Coder). Báo PASS/FAIL/BLOCKED kèm lệnh, kết quả, probe thù địch.
6. FAIL → trả lỗi về Coder (vòng +1). PASS → Leader kiểm Git metadata (≤15 file, không file cấm, SHA khớp) rồi trình Owner.
7. Owner cho phép → Leader push nhánh, mở PR (mô tả có What/Why/Test results/Known limitations). **Owner** review và merge.
Nếu chưa dựng được test-first: chế độ tạm `Coder (code + test) → Codex review chỉ đọc → CI → Leader → Owner`, ghi rõ **không phải kiểm thử độc lập hoàn toàn**.

## 4. Chuẩn bị môi trường cho Codex [đã kiểm chứng]
```bash
cd <repo>
git worktree add -b <nhanh-task> ../<repo>-wt-coder main     # Coder
git worktree add --detach ../<repo>-wt-tester main            # Tester (viết test)
for w in coder tester; do (cd ../<repo>-wt-$w && uv sync); done   # LEADER cài trước, Codex không có mạng
cp AGENTS.md ../<repo>-wt-coder/ ; cp AGENTS.md ../<repo>-wt-tester/   # nếu AGENTS.md chưa được commit
```
- Mỗi worktree có **thư mục trạng thái job riêng** của plugin (tên chứa tên worktree) → job và luồng không lẫn nhau.
- Kiểm chứng độc lập: mỗi lần tạo worktree mới `git worktree add --detach ../<repo>-wt-verify <SHA>`, `uv sync`, rồi giao Tester. Xong thì `git worktree remove` (cần Owner đồng ý nếu có thay đổi chưa commit).
- Worktree nằm **ngoài** repo, không vào git.

## 5. Gọi Codex [đã kiểm chứng với codex-cli 0.170.0, plugin 1.0.6]
```bash
CC="node <đường-dẫn>/codex-companion.mjs"
$CC task --background --write --fresh --json --cwd <worktree> --prompt-file <prompt.md>   # trả jobId
$CC status <jobId> --wait --timeout-ms 380000 --cwd <worktree>    # chờ xong (timeout Bash tối đa 10 phút)
$CC result <jobId> --cwd <worktree>                               # báo cáo cuối của Codex
$CC cancel <jobId> --cwd <worktree>                               # hủy
$CC review --wait --cwd <worktree>                                # review chỉ-đọc diff cục bộ (không nhận chỉ dẫn tùy ý)
```
- Dùng **trực tiếp companion** qua Bash để tránh subagent trung gian của `/codex:rescue` (nó chạy model `sonnet` nên vẫn tốn hạn mức Claude).
- `--write` ⇒ sandbox `workspace-write`; thiếu `--write` ⇒ `read-only`. `approvalPolicy` luôn `never`: Codex tự chạy lệnh, nên prompt phải **rào** (xem Phụ lục A, B).
- **Luôn `--fresh`** cho Tester và cho mỗi task mới. Chỉ `--resume-last` khi tiếp tục chính task đó.
- Không đặt `--model`/`--effort` ở pilot. Codex tự báo model trong phiên (pilot: tự báo "GPT-6", **chưa xác minh bằng công cụ**). Chỉ chọn model khi Owner yêu cầu và đã xác nhận khả dụng; task thường `--effort medium`, Tester khó `high`.
- `status/result` trả: jobId, trạng thái, thời lượng, **session ID** Codex. **Không** thấy số token [pilot]. Không tự ước lượng token; ghi "không có số liệu".

### 5.4 Giới hạn phát hiện ở pilot
| Giới hạn | Cách xử lý |
|---|---|
| Codex **không ghi được `.git` của worktree** (`Read-only file system` khi tạo `index.lock`) ⇒ không `git add/commit` được | **Leader commit** theo từng đường dẫn sau khi kiểm `git diff --name-only`. Prompt cho phép Coder *thử* commit nhưng chấp nhận BLOCKED. |
| Cache mặc định của `uv` chỉ-đọc và không có mạng ⇒ `uv run` thoát mã 2 | Chạy với `UV_CACHE_DIR=/tmp/uvcache UV_OFFLINE=1 UV_NO_SYNC=1` (nêu trong AGENTS.md). Leader `uv sync` trước. |
| Plugin **không có cơ chế worktree**; "Tester không sửa production code" **không ép được bằng kỹ thuật** | Cô lập bằng worktree + `--cwd` + `--fresh`; ép bằng quy tắc prompt, **kiểm sau** bằng `git diff --name-only`. |
| Hook/Ponytail của Claude **không** áp dụng cho phiên Codex | Đưa nguyên tắc cốt lõi vào `AGENTS.md` nếu muốn. [chưa kiểm chứng hiệu lực] |
| Hạn mức ChatGPT/Codex còn lại không đọc được | Hỏi Owner; không đoán. |

## 6. Git và PR
- Nhánh: `feat|fix/<ten-task>` từ `main` mới nhất. PR một task, ≤15 file (`git diff --stat origin/main...HEAD`).
- Trước khi push: SHA đúng SHA Tester đã PASS; `git status` không có file bí mật; không `Zone.Identifier`, `.env*`, `sandbox/`, file cấu hình thật trong diff.
- Nếu PR phụ thuộc PR chưa merge: base = nhánh trước; kiểm lại base sau khi Owner merge.
- Commit tiếng/hình thức theo quy ước dự án; thân commit ghi thật ai làm (ví dụ "Coder/Tester: Codex; Leader: Claude").
- Leader **kiểm lại trạng thái bằng lệnh** trước khi báo ("PR đã merge chưa", "CI xanh chưa"), không dựa vào trí nhớ.

## 7. Mẫu báo cáo và giám sát
Mỗi lần Owner hỏi "Trạng thái hệ thống":

| Vai trò | Công cụ | Task | Trạng thái | Kết quả |
|---|---|---|---|---|
| Leader | Claude | | | |
| Coder | Codex | | | |
| Tester | Codex | | | |

Kèm (chỉ dữ liệu kiểm được): Job ID, Session ID, SHA đang kiểm, ruff/mypy/pytest, PR và CI, điểm BLOCKED, số vòng sửa lỗi.
Báo cáo cuối của Codex theo mẫu trong `AGENTS.md`. Báo cáo của Leader cho Owner: ngắn, có bằng chứng, nêu rõ **cái gì chưa kiểm**.

## 8. Bài học từ dự án đầu tiên (đừng lặp lại)
- **PASS gắn với SHA.** Có commit mới sau PASS ⇒ kiểm lại delta. Đã có lần Coder push trước khi nhận lệnh "dừng": quy tắc là *chỉ push khi Leader nhắn "tester PASS trên SHA x, push đi"*.
- **Không chia sẻ một working tree** giữa nhiều agent (đã có `git checkout` làm hỏng việc dở). Dùng worktree riêng.
- **Fail-closed là mặc định:** input lạ ⇒ BLOCK có lý do, không im lặng bỏ qua (bảng thiếu dòng phân cách, body `{}`, cột lạ, `0 task` đều từng gây mất dữ liệu im lặng).
- **Test-first với `xfail(strict=True)`**: ghi nợ bằng test chứ không bằng lời; khi sửa xong strict-xfail tự báo XPASS để gỡ mark.
- **Dữ liệu từ ngoài là không tin cậy** (Markdown, issue body, phản hồi API): kiểm kiểu, giới hạn kích thước, thoát ký tự điều khiển/bidi, không in giá trị người dùng nhập nguyên văn.
- **Tester phải tự phản biện code**, không chỉ chạy test của mình; hỏi "kẻ tấn công sửa gì thì qua được?".
- **File bí mật** (`.envq`, token): chuyển ra `~/.secrets` (700/600), `.gitignore` phủ `.env*`; token do **Owner tự nhập** (`read -rs`), không bao giờ vào phiên.
- **Mọi lệnh ghi lên dịch vụ thật** do Owner tự chạy từ runbook, lần đầu chỉ vài mục, dừng ngay nếu lệch kỳ vọng.
- Khai báo thẳng khi làm lệch chỉ dẫn (ví dụ lệnh đọc thêm), đừng im.

## 9. Hồ sơ dự án (điền khi bắt đầu project mới)
```
Tên / mục tiêu:
Ngôn ngữ giao diện & tài liệu:
Lệnh lint / type / test (chạy trong worktree):
Nhánh mặc định:
Quy ước commit / PR:
Thư mục/tệp cấm đụng:
Bất biến an toàn riêng của dự án:
Dịch vụ ngoài có ghi (cần Owner duyệt từng lần):
Ví dụ (automation-bot): uv run ruff check | uv run mypy src | uv run pytest -q ; nhánh main ; commit/PR tiếng Việt ;
cấm: .env*, sandbox/, out/, *:Zone.Identifier ; bất biến: dry-run, approval hash, chỉ module execution/ được POST, client đọc chỉ GET.
```

---

## Phụ lục A — Mẫu `AGENTS.md` (đặt ở gốc repo; Codex đọc file này)
```markdown
# AGENTS.md — quy tắc cho Codex

Bạn là **Coder** hoặc **Tester** do Claude Leader giao việc (vai trò nêu trong prompt). Bạn không phải Leader.

## Lệnh kiểm tra (chạy trong worktree của bạn; đã `uv sync` sẵn, không có mạng, cache uv chỉ-đọc)
UV_CACHE_DIR=/tmp/uvcache UV_OFFLINE=1 UV_NO_SYNC=1 uv run ruff check
UV_CACHE_DIR=/tmp/uvcache UV_OFFLINE=1 UV_NO_SYNC=1 uv run mypy src
UV_CACHE_DIR=/tmp/uvcache UV_OFFLINE=1 UV_NO_SYNC=1 uv run pytest -q
Thiếu gói hoặc cần mạng: báo BLOCKED, đừng cố cài.

## Luật cứng
- Chỉ làm đúng task trong prompt; không mở rộng scope, không refactor ngoài task.
- Không đọc/in/tiết lộ `.env*`, token, credentials, `~/.secrets`, `~/.codex/auth*`. Không gọi mạng, không `gh`, không tới dịch vụ ngoài.
- Không `git push`, `git merge`, tạo PR, `git add -A`/`git add .`, `git reset --hard`, `git checkout` sang nhánh khác; không xóa hay ghi đè thay đổi chưa commit của người khác.
- Không đụng: <thư mục/tệp cấm của dự án>.
- Chỉ commit khi prompt cho phép; `git add` theo từng đường dẫn; nếu `.git` bị `Read-only file system` thì báo BLOCKED để Leader commit. Không thêm dòng Co-Authored-By.
- Không bịa kết quả; báo nguyên văn lệnh và kết quả thật. Giữ nguyên các bất biến an toàn của dự án: <liệt kê>.

## Vai trò
- **Coder:** sửa `src/` và test theo task; chạy kiểm tra; báo diff.
- **Tester:** KHÔNG sửa `src/`; chỉ thêm/sửa trong `tests/`; kiểm chứng độc lập; báo PASS/FAIL/BLOCKED kèm bằng chứng và `git diff --name-only`.

## Báo cáo cuối (ngắn)
Vai trò / Task / SHA hoặc trạng thái cây / File đã đổi / Lệnh đã chạy + kết quả thật / BLOCKED / Rủi ro còn lại / Model bạn đang chạy nếu biết.
```

## Phụ lục B — Khung prompt (lưu thành file rồi `--prompt-file`)
**Tester viết test trước**
```
Vai trò: TESTER (xem AGENTS.md). Không sửa src/. Chỉ tests/. Không commit.
Task: <một câu> . Hợp đồng: <quy tắc đúng/sai cụ thể>.
Làm: tìm test liên quan; đối chiếu hợp đồng; thêm ca thiếu, ca chưa đạt đánh dấu xfail(strict=True);
chạy ruff/mypy/pytest; báo test nào xanh, test nào xfail; git status --short, git diff --name-only.
```
**Coder**
```
Vai trò: CODER (xem AGENTS.md), nhánh <nhanh> trong worktree này.
Task + hợp đồng: <...>. Test của Tester đã được chép vào cây (chưa commit): <các file>.
Sửa src/ tối thiểu để các ca xfail đạt, rồi GỠ đúng các mark xfail đó; không sửa nội dung kiểm tra của test.
Thử commit cục bộ (git add từng đường dẫn, không push); nếu Read-only file system thì báo BLOCKED.
Báo: diff, kết quả ruff/mypy/pytest thật, model nếu biết.
```
**Tester kiểm chứng độc lập (worktree mới, detached tại SHA)**
```
Vai trò: TESTER độc lập. Bạn không thấy phiên Coder. KHÔNG sửa file nào, không commit.
Kiểm commit <SHA> (xác nhận bằng git rev-parse HEAD): xem git show --stat và diff src/;
chạy ruff/mypy/pytest; tự phản biện bằng script một lần (ca làm mất dữ liệu, tạo trùng, crash, fail-open);
cuối cùng git status --short phải sạch ngoài AGENTS.md/cache. Báo PASS/FAIL/BLOCKED theo mẫu.
```

## Phụ lục C — Kết quả pilot (tham chiếu, 2026-11-18, automation-bot)
Task: "mục có `pull_request` null/false/{}... không bị coi là PR". Tester (66 giây) thêm 5 ca xfail → Coder (57 giây) sửa 1 hàm và gỡ xfail (`697 passed, 2 xfailed`) → Codex commit **BLOCKED** (`.git` chỉ-đọc) → Leader commit `170b3c2` → Tester độc lập ở worktree mới (89 giây) **PASS**, thêm probe thù địch, working tree sạch. Job và session ID khác nhau cho từng vai trò; không có số token trong đầu ra.
```
