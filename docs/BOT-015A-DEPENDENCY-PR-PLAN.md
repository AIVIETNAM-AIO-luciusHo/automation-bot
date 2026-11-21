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

## Xuất bản draft PR từ commit đã duyệt

Sau khi tạo commit cục bộ, dùng plan gốc để lập plan xuất bản. Bước này
đọc GitHub để xác nhận repo và đọc Git remote để đối chiếu base và head.
Cấp `GITHUB_TOKEN` qua môi trường bằng cơ chế quản lý bí mật của Owner.

```sh
uv run github-bot dependencies pr-publish-plan --workspace . \
  --plan dependency-plan.json --base main > dependency-publish-plan.json
```

Chỉ tiếp tục nếu lệnh thoát mã 0. Kiểm tra repo, SHA của base và head,
tiêu đề và plan nguồn trong JSON. Owner duyệt đúng `plan_sha256` của plan
xuất bản. Khi Owner cho phép ghi, dùng token giới hạn đúng repo với quyền
đẩy nhánh và tạo PR.

```sh
uv run github-bot dependencies pr-publish --workspace . \
  --plan dependency-publish-plan.json --approve-hash SHA256_DA_DUYET --write
```

Lệnh chỉ tạo remote head nếu ref chưa tồn tại. Nếu ref đã đúng SHA, lệnh
đối chiếu PR hoặc tạo draft PR mà không push lại. Nhánh đã có SHA khác,
base đã đổi, URL rewrite trong config Git cục bộ hoặc PR xung đột đều làm
lệnh dừng. Sau timeout hoặc lỗi có kết quả chưa rõ, kiểm tra nhánh và danh
sách PR trước khi chạy lại cùng plan đã duyệt. Lệnh kiểm tra lại các ref
và PR trước lần ghi tiếp, và trả `existing` nếu tìm thấy draft khớp.

Kiểm chứng hiện tại dùng bare repo cục bộ và HTTP mock. Quyền token,
xác thực HTTPS và kết quả trên GitHub thật cần Owner kiểm tra.

### Quyền token và xác nhận CI của draft PR

Với cách chạy CLI do Owner kiểm soát, dùng fine-grained PAT giới hạn ở
repo đích đã duyệt. Bước preview cần `Contents: Read-only` và quyền
Metadata đọc để xác minh repo và ref qua HTTPS. Bước publish cần
`Contents: Read and write` để tạo ref và `Pull requests: Read and write`
để đối chiếu rồi tạo draft PR. Cấp token tương ứng qua `GITHUB_TOKEN`
cho từng lần gọi. Phase này chưa cấu hình token, repo thật hay workflow ghi.

CI hiện chỉ nhận push vào `main` và pull request nhắm vào `main`. Push
lên nhánh dependency không đủ để xác nhận CI đã chạy. Sau lần thử được
Owner duyệt, mở tab **Checks** của draft PR, tìm job `CI / test` và đối
chiếu run với đúng PR và head SHA đã duyệt. Nếu runner kiểm merge ref,
đối chiếu head SHA của PR cùng SHA checkout trong run. Nếu thiếu run,
check đang chờ hoặc sai SHA, ghi nhận CI chưa được xác minh và giữ PR
chờ review. Kiểm tra ref, sự kiện và danh tính token đã tạo PR. Nếu sau
này chạy từ Actions, cần kiểm trên hosted runner việc `GITHUB_TOKEN`
của workflow hoặc App token/PAT đã chọn có kích hoạt được check dự kiến.

Với dependency ảnh hưởng mô hình hoặc xử lý dữ liệu, ví dụ NumPy,
scikit-learn, Pillow hoặc PyTorch, cần test hồi quy phù hợp hoặc reviewer
có chuyên môn trước khi chấp nhận thay đổi. Giữ draft và các required
check hiện hành trong quá trình kiểm.
