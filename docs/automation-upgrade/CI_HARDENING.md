# Kiểm tra và khôi phục CI

CI giữ workflow **CI** và job **test**. Workflow chạy khi có push lên
`main` hoặc pull request nhắm vào `main`. Quyền GitHub là `contents: read`,
checkout không giữ credential và job CI cũ cùng workflow/ref có thể bị hủy.
Realcheck vẫn chỉ chạy thủ công, dùng token đọc ở bước plan và upload đúng
ba tệp khi kết quả khớp fixture.

Cả hai workflow dùng các commit SHA của Actions đã được đối chiếu trong
[GAP_ANALYSIS.md](GAP_ANALYSIS.md), uv 0.13.6 và Python 3.13. CI dùng cache uv
theo `uv.lock` và chỉ lưu cache khi push vào `main`. Realcheck tắt cache cho
smoke thủ công. CI tải actionlint v1.7.12 cho Linux amd64 và kiểm SHA-256
trước khi giải nén hay chạy.

## Kiểm tra cục bộ

```sh
uv sync --locked
uv run --no-sync ruff check
uv run --no-sync mypy src
uv run --no-sync pytest
uv run --no-sync github-bot --help
uv run --no-sync github-bot issues validate --help
actionlint
```

Smoke đi qua entrypoint đã cài sau `uv sync`. Kiểm tra build wheel riêng
nằm ngoài phạm vi thay đổi CI này.

Kiểm chứng offline của Phase 1 dùng venv có sẵn. Ruff, Mypy và hai lệnh
help đạt. Pytest trả `740 passed, 1 xfailed`. `uv lock --check` đạt.
Cài mới bằng `uv sync --locked` trên bản sao repo bị chặn do cache offline
thiếu wheel `packaging==26.3`. Một fixture tối thiểu trong `/tmp` cài với
lock hợp lệ thành công. Sau khi đổi metadata, `uv sync --locked` thoát 1
vì lock cần cập nhật. Ruff trả mã 1 với Python sai cú pháp trong bản sao
thử nghiệm. `bash -n` đạt cho 6 bước CI và 4 bước Realcheck. Archive
giả sai checksum bị chặn trước khi giải nén. Actionlint 1.7.12 đã kiểm cả hai
workflow bằng binary do Leader xác minh. Chưa kiểm tải actionlint, cache
hay chạy hai workflow trên runner GitHub.

## Required check và rollback

Sau hosted run đầu tiên trên đúng SHA, Owner xác nhận check **CI / test**
và tên context chính xác mà GitHub hiển thị trước khi đặt required check
cho `main`. Đối chiếu PR hợp lệ, PR có lỗi và lần chạy trên `main`.
Realcheck là smoke thủ công, có thể được Owner chạy riêng để kiểm đủ ba
artifact.

Nếu SHA của Action hoặc runner mới làm CI hỏng, khôi phục phiên bản đã
chạy tốt bằng PR hoàn tác diff workflow. Giữ tên workflow/job, quyền chỉ
đọc và các ruleset bảo vệ. Xác nhận lại check trên SHA của PR rollback
trước khi merge theo quy trình hiện hành. Phase 1 không thêm luồng ghi
GitHub nên rollback được giới hạn ở cấu hình workflow.
