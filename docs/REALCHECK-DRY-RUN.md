# Dry-run thủ công trên realcheck

Owner cấu hình trong phần Actions secrets and variables của repo chứa CLI:

- Secret `REALCHECK_READ_TOKEN` chứa fine-grained token giới hạn cho
  `AIVIETNAM-AIO-luciusHo/realcheck`, với quyền repository `Issues: Read-only`
  và quyền Metadata đọc bắt buộc của GitHub. Secret chỉ được truyền vào bước lập plan.
- Biến `REALCHECK_ASSIGNEE` chứa login thử nghiệm có thể được gán issue trong
  `realcheck`. Workflow dùng login này để tạo danh bạ tối thiểu cho fixture giả.

Sau khi Owner cho phép chạy thật, chọn **Actions → Realcheck dry-run → Run
workflow** từ ref đã được duyệt. Workflow chỉ chạy thủ công. Bước validate
chạy offline, rồi bước plan gọi GitHub bằng GET. Giữ nguyên đường dẫn tương đối
`tasks.md` và ID `TASK-012` để khớp marker BOT-012.

Kỳ vọng 1 `SKIP` cho issue #202, 0 `CREATE` và 0 `BLOCK`. Artifact
`realcheck-dry-run` chứa đúng `PREVIEW.md`, `PREVIEW.json` và `plan.json`.
Nếu token bị thiếu, đầu vào lỗi, plan có `BLOCK` hoặc kết quả khác kỳ vọng,
workflow dừng trước khi tải artifact. Nếu dữ liệu thử nghiệm hoặc issue #202
thay đổi, báo Leader để đối chiếu trước khi chạy lại.
