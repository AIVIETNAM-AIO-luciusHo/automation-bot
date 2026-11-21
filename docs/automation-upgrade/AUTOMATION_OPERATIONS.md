# Vận hành và khôi phục automation

Tài liệu này chuẩn bị cho PR 5. Phase 3 (approved issue apply) và Phase 4
(dependency draft PR) đang review tại thời điểm soạn. GitHub App, reusable
workflow và multi-repo chưa được triển khai.

## Cổng trước lần ghi thật

- **P0 ổn định.** Xác nhận CI, preview PR và Realcheck dry-run trên SHA
  đã duyệt. Giữ check `CI / test`, policy và required checks. Xác minh
  môi trường ghi có required reviewer độc lập, chỉ từ `main`, và từ chối
  khi thiếu approval, source SHA hoặc provenance.
- **Issue apply.** Owner đối chiếu repo, source SHA, run/attempt, plan
  hash và hành động trước khi duyệt. Trên repo thử nghiệm, kiểm chạy lại
  tuần tự, timeout sau POST và audit partial failure. Chỉ cho phép ghi
  khi đã có cách kiểm marker và quy trình dừng.
- **Demo Phase 4.** Owner duyệt riêng repo thử nghiệm, base/head SHA,
  plan hash và token. Xác minh chỉ tạo ref vắng và draft PR đúng base,
  head, SHA. Chạy lại phải nhận PR hiện có. Đối chiếu downstream CI trên
  đúng PR và SHA đã duyệt. Nếu thiếu check, giữ draft chờ review và xác
  minh loại sự kiện, target `main` cùng danh tính tạo PR. Với dependency
  nhạy cảm, ghi nhận regression test hoặc người review có chuyên môn.
- **Mở rộng có giới hạn.** Chỉ thử App hoặc workflow tái sử dụng sau khi
  có bằng chứng trên cho một repo. Kiểm một target được phép và một
  target bị từ chối trước khi mở rộng. Owner duyệt riêng mọi thay đổi
  quyền, installation và target.

## Dừng, đối chiếu và khôi phục

1. Khi thấy repo/SHA/hash lệch, approval thiếu, quyền không đủ, timeout
   hoặc audit dở, dừng ghi tiếp. Giữ run ID, actor/reviewer, plan hash,
   SHA, repo và mã lỗi đã lọc. Loại token, nội dung request và phản hồi
   chưa lọc khỏi log chia sẻ.
2. Với issue, tìm marker theo `repo + source_path + task_id` và đối
   chiếu số issue với audit. Nếu đã tạo, lập plan chỉ đọc để xác nhận
   `SKIP`. Nếu trạng thái còn mơ hồ hoặc có marker trùng, chuyển Owner
   đối chiếu trước khi duyệt lần ghi mới.
3. Với draft PR, đọc remote head và danh sách PR của head. Nếu head
   đúng SHA nhưng chưa xác minh được PR, ghi nhận trạng thái dở và kiểm
   lại trước khi chạy lại. Nếu draft khớp repo, base, head và SHA, giữ
   số PR đã có. Nếu ref hoặc PR xung đột, dừng để Owner xử lý.
4. Nếu workflow hoặc credential có vấn đề, Owner tạm ngừng đường ghi
   và thu hồi credential bị ảnh hưởng. Dùng PR revert để khôi phục
   cấu hình đã kiểm, giữ required checks, rulesets và review. Revert
   workflow không xóa issue, draft PR hay ref đã được tạo.
5. Chỉ mở lại sau khi đối chiếu remote, sửa nguyên nhân, chạy kiểm đọc
   trên SHA mới và có phê duyệt mới gắn với đúng plan. Lưu bằng chứng
   đã lọc theo chính sách lưu trữ của repo.

Concurrency của Actions chỉ serialize job trong cùng repo và group.
Trong thời gian có job ghi, tránh chạy CLI hoặc workflow ghi thứ hai
vào cùng target. Marker và GET trước POST giảm rủi ro trùng khi chạy lại
tuần tự. Ghi qua nhiều repo hoặc runner độc lập cần một cơ chế điều
phối chung trước khi cam kết chống trùng.

Đọc thêm [CI và rollback](CI_HARDENING.md), [preview PR](TASK_PREVIEW.md)
và [ma trận quyền](GITHUB_APP_ROADMAP.md).
