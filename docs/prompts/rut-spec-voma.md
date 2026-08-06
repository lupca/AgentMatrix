Rút spec sống cho project VOMA và ghi vào bảng spec_item qua MCP.

## Việc của bạn

Đọc code thật của project, rút ra các mệnh đề mô tả **hệ thống đang có**, rồi
ghi vào bảng `spec_item` bằng tool `spec_write`.

Mục đích cuối: sau này hỏi *"thêm chức năng X có trùng hay xung đột với cái gì
không"* thì tra được. Nếu spec bạn viết không trả lời được câu đó thì nó vô dụng.

## Làm project nào

Lần này làm 3 project sau, theo đúng thứ tự:

    voma-identity      /home/lupca/projects/voma/voma-identity
    voma-gateway       /home/lupca/projects/voma/voma-gateway
    topvnsport-oms     /home/lupca/projects/voma/voma-oms

Xong 3 cái thì **DỪNG và báo cáo**. Đừng tự làm tiếp 10 project còn lại.
Lý do ở mục "Vì sao chỉ làm 3" bên dưới.

Danh sách đầy đủ 13 project VOMA để bạn biết bối cảnh, KHÔNG phải để làm hết:

    voma                /home/lupca/projects/voma
    voma-identity       /home/lupca/projects/voma/voma-identity
    voma-super-admin    /home/lupca/projects/voma/voma-super-admin
    voma-gateway        /home/lupca/projects/voma/voma-gateway
    voma-dev            /home/lupca/projects/voma/voma-dev
    voma-invoice        /home/lupca/projects/voma/voma-invoice
    voma-agentic-test   /home/lupca/projects/voma/voma-agentic-test
    voma-qa-agent       /home/lupca/projects/voma
    topvnsport-oms      /home/lupca/projects/voma/voma-oms
    topvnsport-wms      /home/lupca/projects/voma/voma-wms
    topvnsport-pmi      /home/lupca/projects/voma/voma-pim
    topvnsport-devops   /home/lupca/projects/voma/voma-infra
    topvnsport-web      /home/lupca/projects/topvnsport.com

## Hiện trạng bảng spec_item

Đang có 16 dòng, nằm cả ở 2 project (`voma-agentic-test` 15, `voma-qa-agent` 1).
11 project còn lại **chưa có gì**.

Trước khi viết cho một project, gọi `spec_get` xem project đó đã có gì chưa,
tránh ghi trùng.

## Cách ghi mỗi spec_item

Gọi `spec_write` với các trường:

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
                asserted  = suy từ tài liệu/tên hàm, CHƯA đối chiếu code

    project_id       id project (cột bên trái danh sách trên)
    derived_from_sha commit sha hiện tại: git -C <repo> rev-parse HEAD
    derived_by       tên/model của bạn

Số lượng: **10-25 spec_item** cho một project cỡ vừa. Đừng cố nhiều. Ít mà đúng
và có dẫn chứng thì hơn hẳn nhiều mà chung chung.

## Tuyệt đối không

- **Đừng bịa spec cho chức năng không có trong code.** Thà thiếu còn hơn sai.
- **Đừng ghi `confidence=verified` nếu chưa mở file ra đọc.** Không chắc thì để
  `asserted`. Ghi sai mức tin cậy hại hơn không ghi, vì người sau sẽ tin nhầm.
- **Đừng sửa bất kỳ file nào trong repo.** Việc này CHỈ ĐỌC repo và GHI vào DB.
- **Đừng dừng lại hỏi khi gặp chỗ không hiểu.** Cứ ghi phần hiểu được, rồi liệt
  kê phần không hiểu trong báo cáo cuối.

## Vì sao chỉ làm 3 project rồi dừng

Chưa ai biết `spec_item` rút theo cách này có thật sự trả lời được câu hỏi
*"chức năng mới có trùng/xung đột không"* hay không.

Nếu khuôn còn lỗi mà nhân ra 13 project thì phải làm lại từ đầu — đúng kiểu lãng
phí đang cần diệt. Làm 3 cái, để người ta xem, sửa khuôn, rồi mới chạy tiếp.

Ba project này chọn có chủ ý:
  voma-identity   repo gọn, graph code đã build sẵn — dùng để chuẩn hoá cách làm
  voma-gateway    điểm tích hợp, spec ở đây giá trị nhất
  topvnsport-oms  nghiệp vụ nặng — thử xem cách làm có chịu nổi repo lớn không

## Lưu ý kỹ thuật

**Graph code phần lớn chưa build.** Chỉ `voma-identity` và `voma-super-admin` ở
trạng thái fresh. Với `voma-gateway` và `topvnsport-oms` thì semantic search sẽ
không có gì — phải đọc file trực tiếp.

Thứ tự đọc nên theo: README → docs/ → định nghĩa route/API → model dữ liệu →
migration → file cấu hình.

## Báo cáo cuối

1. Số `spec_item` đã tạo, tách theo từng `project_id` và từng `kind`.
2. Tỉ lệ `verified` so với `asserted`.
3. Phần nào của hệ thống bạn **không đọc được hoặc không hiểu** — ghi thẳng ra,
   đây là thông tin quan trọng nhất của báo cáo.
4. Đánh giá của chính bạn: với spec vừa viết, có trả lời được câu *"thêm chức
   năng X có trùng/xung đột không"* chưa? Nếu chưa thì thiếu gì?

## Mẫu tham khảo

/home/lupca/Documents/agmx/02_spec_design/
    Logical design chức năng [ Tên chức năng ].docx.txt
    Physical design_chức năng [ Tên chức năng ].docx.txt
    Tiền đề physical design_chức năng [ Tên chức năng ].docx.txt
    Tiền đề thiết kế chức năng [ Tên chức năng ].docx.txt

Đây là mẫu tài liệu cho NGƯỜI đọc, không phải khuôn cho `spec_item`.
Dùng để biết nên rút những khía cạnh nào — đừng bê nguyên cấu trúc file vào DB.
