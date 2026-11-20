# Báo cáo pull request Dependabot

Cấp `GITHUB_TOKEN` qua môi trường. Dùng fine-grained token giới hạn cho repo
cần báo cáo, với quyền `Pull requests: Read-only`.

```sh
uv run github-bot dependabot report --repo OWNER/REPO
uv run github-bot dependabot report --repo OWNER/REPO --group 'pip=Ứng dụng' --group 'npm=Giao diện'
```

Lệnh dùng GET để liệt kê pull request đang mở do `dependabot[bot]` có loại
tài khoản `Bot` tạo và in báo cáo Markdown. Nhóm được suy ra từ nhánh
`dependabot/<ecosystem>/…`. Mặc định `pip` và `uv` vào **Runtime**,
`github_actions` và `github-actions` vào **CI**, còn lại vào **Chưa phân nhóm**.
Dùng `--group` nhiều lần để thêm hoặc ghi đè ánh xạ. Dấu gạch ngang và gạch
dưới được chuẩn hóa về cùng một khóa, mỗi khóa tùy chỉnh chỉ được khai báo một lần.

Nếu một trang API lỗi, dữ liệu cần cho báo cáo sai định dạng hoặc danh sách
vượt giới hạn phân trang, lệnh trả mã lỗi và không in báo cáo một phần.
