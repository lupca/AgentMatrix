Rút spec sống cho MỘT project và ghi vào bảng spec_item qua MCP.

## Project của bạn

    project_id : __PROJECT_ID__
    repo_root  : __REPO_ROOT__

**CHỈ làm project này.** Đừng đụng project khác — có agent khác đang làm song song,
giẫm chân nhau sẽ sinh spec trùng.

## Việc của bạn

Đọc code thật, rút ra các mệnh đề mô tả **hệ thống đang có**, ghi vào `spec_item`
bằng tool `spec_write`.

Mục đích cuối: sau này hỏi *"thêm chức năng X có trùng hay xung đột với cái gì
không"* thì tra được. Nếu spec bạn viết không trả lời được câu đó thì nó vô dụng.

Trước khi viết, gọi `spec_get(filter={"project_id": "__PROJECT_ID__"})` xem đã có
gì chưa, tránh ghi trùng.

## Cách ghi mỗi spec_item

    kind        requirement | constraint | design
                  requirement : hệ thống PHẢI làm gì (nghiệp vụ)
                  constraint  : ràng buộc không được vi phạm
                                (bảo mật, dữ liệu, tích hợp, hiệu năng)
                  design      : quyết định thiết kế VÀ lý do chọn

    title       Một câu, tối đa 300 ký tự. Nói rõ chủ thể.
                Xấu : "Xử lý xác thực"
                Tốt : "voma-identity cấp JWT hết hạn sau 15 phút, refresh token 7 ngày"

    body        Đủ để người chưa đọc code hiểu được.
                BẮT BUỘC dẫn đường dẫn file cụ thể làm chứng cứ.

    confidence  verified  = đã MỞ FILE ĐỌC tận nơi
                asserted  = suy từ tài liệu hoặc tên hàm, CHƯA đối chiếu code

    project_id        __PROJECT_ID__
    derived_from_sha  git -C __REPO_ROOT__ rev-parse HEAD
    derived_by        tên/model của bạn

**Số lượng: 10-25 spec_item.** Đừng cố nhiều. Ít mà đúng và có dẫn chứng thì hơn
hẳn nhiều mà chung chung. Repo nhỏ thì 10 cũng được — đừng nống lên cho đủ số.

## Chỗ nên đào (theo thứ tự)

README → docs/ → định nghĩa route/API → model dữ liệu → migration → file cấu hình.

Ưu tiên những thứ **service khác phải biết**: endpoint công khai, header nhận/gửi,
định dạng dữ liệu trao đổi, cơ chế xác thực, ràng buộc tenant/seller. Đó là chỗ
xung đột liên service hay nằm.

## Tuyệt đối không

- **Đừng bịa spec cho chức năng không có trong code.** Thà thiếu còn hơn sai.
- **Đừng ghi `confidence=verified` nếu chưa mở file ra đọc.** Không chắc thì để
  `asserted`. Ghi sai mức tin cậy hại hơn không ghi, vì người sau sẽ tin nhầm.
- **Đừng sửa bất kỳ file nào trong repo.** Việc này CHỈ ĐỌC repo và GHI vào DB.
- **Đừng dừng lại hỏi** khi gặp chỗ không hiểu. Ghi phần hiểu được, liệt kê phần
  không hiểu trong báo cáo cuối.

## Báo cáo cuối

1. Số `spec_item` đã tạo, tách theo `kind`. **Kiểm lại phép cộng** — báo cáo đợt
   trước cộng sai hai lần.
2. Tỉ lệ `verified` so với `asserted`.
3. Phần nào của hệ thống bạn **không đọc được hoặc không hiểu**. Ghi thẳng ra —
   đây là thông tin quan trọng nhất của báo cáo.
4. Endpoint / header / định dạng dữ liệu nào project này **trao đổi với service
   khác**. Liệt kê cụ thể, kể cả khi chưa chắc bên kia dùng thế nào.
