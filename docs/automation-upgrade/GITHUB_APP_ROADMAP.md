# Lộ trình GitHub App và workflow tái sử dụng

**Trạng thái tại `9fa4263`.** PR 5 chỉ bổ sung tài liệu. GitHub App,
reusable workflow và hỗ trợ nhiều repo chưa được triển khai. Hai luồng
ghi được mô tả dựa trên các PR Phase 3 và Phase 4 đang review. Baseline
hiện có CI, preview offline và Realcheck dry-run chỉ đọc.

## Quyền tối thiểu theo tác vụ

Bảng này là phạm vi đề xuất để Owner kiểm chứng trên repo thử nghiệm.
Mỗi job nhận quyền của đúng tác vụ và đúng repo. Metadata đọc là quyền
cơ bản của App trên repo được cấp.

| Tác vụ | Repo | Quyền cần cấp | Danh tính dự kiến |
|---|---|---|---|
| CI và preview offline trên PR | Repo chứa CLI | `Contents: Read` cho checkout | Token Actions chỉ đọc. CLI preview không dùng token. |
| Lập plan issue có GET | Repo đích | `Issues: Read`, Metadata đọc | Token đọc giới hạn target. |
| Kiểm SHA `main` và lịch sử approval | Repo chứa workflow | `Contents: Read`, `Actions: Read` | Token Actions của job preflight. |
| Apply issue đã duyệt | Repo đích | `Issues: Read and write`, Metadata đọc | Hiện đề xuất secret environment của Phase 3. Sau này có thể cấp installation token cho bước apply. |
| Preview publish dependency | Repo đích | `Contents: Read`, Metadata đọc | Token giới hạn target cho Git HTTPS và xác minh repo. |
| Đẩy nhánh mới và tạo draft PR đã duyệt | Repo đích | `Contents: Read and write`, `Pull requests: Read and write`, Metadata đọc | Fine-grained PAT do Owner cấp cho thử nghiệm Phase 4. App token là phương án sau khi kiểm downstream CI. |

Tách credential đọc khỏi credential ghi. Tránh cấp cho cùng installation
quyền ghi Issues và PR nếu hai tác vụ có thể tách riêng. Giới hạn token
được cấp theo tập repo và quyền của từng lần chạy, trong phạm vi App đã
được cấp. Kiểm quyền âm tính trên repo ngoài allowlist và thử một lần chỉ
đọc trước khi cho phép ghi.

## Installation và quản lý key

1. Giữ allowlist trong cấu hình do Owner kiểm soát, với repo ID, owner/repo
   dự kiến và installation ID tương ứng. Từ chối target kiểu URL hoặc
   target chỉ xuất hiện trong Markdown, PR input hay cấu hình của fork.
2. Trước khi cấp token, đối chiếu App ID và installation với tài khoản
   đã duyệt. Xác minh installation nhìn thấy đúng repo ID và chỉ cho phép
   scope cần thiết. Sai owner, repo đổi tên, installation đã bị thu hồi
   hoặc dữ liệu thiếu đều yêu cầu dừng và đối chiếu lại cấu hình.
3. Giữ private key ở secret store được bảo vệ, chỉ cung cấp cho bước cấp
   token trong job đã được duyệt. Tạo installation token ngắn hạn sát
   lúc sử dụng và kiểm `expires_at`. Giữ key, JWT và token ngoài stdout,
   output của job, artifact và URL Git. Khi token hết hạn, kiểm lại
   approval, target và remote state trước khi cấp token thay thế.
4. Owner xoay key bằng cách cấp key mới, kiểm trên repo thử nghiệm rồi
   thu hồi key cũ. Nếu nghi ngờ lộ key, vô hiệu hóa luồng ghi, thu hồi
   token hoặc installation bị ảnh hưởng, thay key và đối chiếu audit
   trước khi cho chạy lại.

## Workflow dùng chung và mở rộng

Chỉ thiết kế `workflow_call` sau khi hai luồng một repo đã ổn định. Dùng
input có kiểu cho mode, repo, source SHA và định danh plan đã duyệt.
Workflow tự xác minh caller/ref, target allowlist, provenance và schema
trước khi dùng credential. Giá trị input hoặc flag `approved=true` không
thay cho bằng chứng phê duyệt. Truyền secret theo tên cần thiết, giữ bước
ghi sau environment gate và tránh `secrets: inherit`.

Pin reusable workflow bằng commit SHA đã review. Tách phiên bản hợp đồng
input/artifact khỏi tên workflow. Khi đổi schema, kiểm caller cũ và hướng
dẫn nâng pin trước khi bỏ hỗ trợ. Mỗi repo giữ policy và installation
mapping riêng. Bắt đầu bằng một repo thử nghiệm, đo phút runner, dung
lượng artifact và mức gọi API trước khi tăng số repo.

Webhook, backend, queue và GitHub Projects nằm sau cổng P0. Nếu có nhu
cầu webhook về sau, thiết kế xác minh chữ ký, chống replay theo delivery,
retry có giới hạn và kiểm vòng lặp do sự kiện do bot phát sinh.

Xem [cổng vận hành và recovery](AUTOMATION_OPERATIONS.md).
