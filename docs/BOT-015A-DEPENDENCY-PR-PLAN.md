# Preview PR cập nhật phụ thuộc

Chạy từ gốc Git worktree sau khi đã stage các bản sửa muốn xem trước.

```sh
uv run github-bot dependencies pr-plan --workspace . --repo OWNER/REPO \
  --branch deps/update --title 'Cập nhật phụ thuộc' \
  --file pyproject.toml --file uv.lock
```

Truyền `--file` cho từng tệp đã sửa, gồm `pyproject.toml` hoặc `uv.lock`.
Index phải chứa đúng các bản sửa đã chọn. Lệnh từ chối thêm, xóa, xung đột,
symlink, tệp staged ngoài danh sách và tệp đã chọn còn thay đổi unstaged.
Nội dung working tree của tệp đã chọn phải khớp blob staged theo byte.

JSON trên stdout chứa repo đích, nhánh, tiêu đề, SHA của HEAD,
đường dẫn cùng mode và blob ID từ index, và `plan_sha256`. Repo đích do
người chạy nhập và chỉ được kiểm tra cú pháp. Lệnh chỉ đọc Git cục bộ,
không xác minh repo qua mạng và không thay đổi index, nhánh hay commit.
