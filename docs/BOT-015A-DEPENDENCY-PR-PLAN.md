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

## Tạo nhánh và commit cục bộ từ plan đã duyệt

Dùng Git 2.43 trở lên và cấu hình danh tính commit trong repo. Lưu stdout của
`pr-plan` vào tệp JSON trong workspace, kiểm tra repo, nhánh, tiêu đề,
HEAD cùng các blob staged, rồi duyệt đúng `plan_sha256`.

```sh
uv run github-bot dependencies pr-apply --workspace . --plan dependency-plan.json \
  --approve-hash SHA256_DA_DUYET --write
```

Lệnh tạo một commit con của HEAD trong plan và đặt nó trên nhánh đã duyệt.
HEAD, Git index và working tree hiện tại giữ nguyên. JSON kết quả trả
`status` (`created` hoặc `existing`), `branch` và `commit_sha`.
Chạy lại cùng plan chỉ trả `existing` nếu HEAD và index còn khớp và commit
trên nhánh có đúng cây, parent và dấu hash trong thông điệp commit.
Nếu nhánh đang được checkout, là symbolic ref hoặc trỏ tới commit khác,
lệnh từ chối. Mọi thao tác diễn ra cục bộ. Nếu cập nhật ref thất bại,
có thể còn object Git chưa được nhánh nào trỏ tới.
