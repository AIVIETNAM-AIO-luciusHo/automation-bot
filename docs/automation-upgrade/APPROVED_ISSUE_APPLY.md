# Duyệt apply issue Realcheck

Workflow **Apply Realcheck issue** chỉ dành cho lần dispatch đầu tiên trên
`main` của repo automation-bot-QA. Plan dùng fixture `tasks.md` của
Realcheck và tối đa một task. Kỳ vọng hiện tại là `SKIP` nếu #202 còn mang
marker cũ. Đối chiếu và dừng nếu thấy `CREATE` bất ngờ.

## Thiết lập do Owner quản lý

- Tạo environment `realcheck-issue-write` với required reviewers, chặn
  tự duyệt, chỉ cho nhánh `main` và vô hiệu hóa bypass nếu có thể.
- Dùng `REALCHECK_ASSIGNEE` và secret `REALCHECK_READ_TOKEN` cho job plan.
  Token plan chỉ cần quyền đọc Issues và Metadata trên repo Realcheck.
- Đặt `REALCHECK_ISSUES_WRITE_TOKEN` chỉ trong environment. Giới hạn token
  cho Realcheck với quyền Issues write và Metadata read. Bước apply là nơi
  duy nhất nhận token này. Job preflight dùng token Actions riêng có
  `actions: read` và `contents: read` của repo nguồn.

## Duyệt đúng run

Đối chiếu tóm tắt và artifact `issue-plan-<run_id>-1`. Kiểm repo đích,
SHA checkout, nguồn, SHA-256 của plan và hành động trước khi reviewer
độc lập duyệt environment. Job apply tải artifact cùng run, kiểm lại
tệp plan và manifest, ref `main` và approval history. Thiếu approval
đúng environment, reviewer trùng người khởi chạy, ref đã đổi hoặc run
được chạy lại đều bị chặn. Tạo dispatch mới để lập và duyệt lại plan.

Audit `issue-apply-audit-<run_id>-1` lưu provenance đã xác minh gồm
reviewer, actor, repo, SHA nguồn và hash. Artifact đi kèm audit JSONL
của CLI khi đã tạo được audit. Nếu lỗi hoặc timeout, kiểm tra marker,
issue và audit trước khi dispatch mới. Các job cùng group Realcheck
trong repo workflow được serialize và job đang ghi không bị group hủy.
CLI bên ngoài hoặc repo khác vẫn có thể gặp race giữa GET và POST.

Các yêu cầu reviewer, nhánh, secret và lịch sử approval cần được Owner
xác minh trên GitHub trước khi cho phép ghi thử. Kiểm thử offline dùng
HTTP giả. Quy trình không thay đổi ruleset hoặc thiết lập GitHub.
