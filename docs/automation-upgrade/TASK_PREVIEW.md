# Preview task offline cho pull request

Workflow **Task preview** chạy trên PR nhắm vào `main` khi backlog, danh bạ
mẫu, workflow hoặc engine liên quan thay đổi. Job dùng quyền `contents: read`
và preview chạy được trên fork mà không cần secret hoặc token cho GitHub API.
Vì có lọc đường dẫn, chưa đặt job này làm required check.

Workflow dùng `docs/IMPLEMENTATION_TASKS.md` và `configs/members.example.yaml`.
Repo đích và danh sách cho phép đều lấy từ `GITHUB_REPOSITORY`, độc lập với
nội dung Markdown. Checkout PR mặc định là merge ref. `source_sha` là kết
quả `git rev-parse HEAD` trên bản checkout được kiểm, có thể khác PR head SHA.

## Kiểm tra cục bộ

```sh
SOURCE_SHA="$(git rev-parse HEAD)"
REPO='OWNER/REPO'
uv run github-bot issues preview --workspace . \
  --input docs/IMPLEMENTATION_TASKS.md --members configs/members.example.yaml \
  --repo "$REPO" --allowed-repo "$REPO" --source-sha "$SOURCE_SHA" \
  --output-dir task-preview
```

Thay `OWNER/REPO` bằng repo muốn kiểm. Cảnh báo `NO_ASSIGNEE` có thể xuất hiện
vì backlog hiện chưa gắn người phụ trách. Dữ liệu lỗi hoặc ngoài allowlist
làm lệnh trả mã khác 0. Workflow chỉ upload artifact khi lệnh thành công.

## Đối chiếu kết quả

- `PREVIEW.md` hiển thị danh sách task đã kiểm cục bộ, người phụ trách phân
  giải từ danh bạ mẫu và nhãn được khai báo.
- `plan.json` mang schema `offline-preview-v1`, `remote_verified: false`,
  dữ liệu task và provenance được băm. So hash với `metadata.json`.
- `metadata.json` ghi SHA checkout, đường dẫn nguồn, repo đích, thời điểm UTC,
  phiên bản CLI và hash. Thời điểm tạo nằm ngoài phần được băm, nên cùng
  đầu vào tạo cùng hash.

Preview chưa đối chiếu quyền gán, sự tồn tại của nhãn hoặc issue trên GitHub.
`issues apply` từ chối schema này ngay cả khi truyền đúng hash và `--write`.
Chỉ dùng artifact PR làm bằng chứng tham khảo trên đúng SHA đã kiểm. Realcheck
vẫn là smoke riêng dùng GET trên repo thử nghiệm.
