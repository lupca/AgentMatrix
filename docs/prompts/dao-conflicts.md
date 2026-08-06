Đào ràng buộc chéo giữa các service VOMA — tìm `conflicts_with` còn thiếu.

## Vì sao đợt này khác đợt trước

`spec_relation` hiện có 91 quan hệ, nhưng chỉ **10 cái là `conflicts_with`** trên
218 spec_item. Đó là loại quan hệ **giá trị nhất** và đang **thiếu nhất**.

Lý do nó giá trị nhất: `conflicts_with` là **ranh giới phủ định** — nó nói
*"đụng vào đây là vi phạm cái kia"*. Khi lập kế hoạch cho một chức năng mới, thứ
cứu được nhiều công nhất không phải "hệ thống có gì" mà là "làm thế này sẽ hỏng
cái gì".

Đợt trước bạn quét chung chung. Đợt này **đào có chủ đích** vào những chỗ xung đột
thật sự hay nằm.

## Bằng chứng cách này hiệu quả

Đợt 2 tìm được 6 `conflicts_with`, **4 trong số đó cụm quanh header xác thực**.
Từ đó lần ra một lỗ hổng thật:

    Identity  auth_request_set chỉ có 6 giá trị, KHÔNG cấp seller_id
    Gateway   prod xoá trắng X-Seller-Id / dev lấy nguyên từ client
    OMS       tin header, chỉ parse_uuid, KHÔNG đối chiếu Identity
    PIM       CÓ validate seller membership qua Identity API

=> Ở dev, client đặt X-Seller-Id tuỳ ý thì OMS khoanh dữ liệu theo seller đó.
   Và OMS lệch chuẩn so với PIM.

Không đọc riêng repo nào thấy được. Phải nối spec mới lộ.

## Bảy chỗ cần đào

Với **mỗi chỗ**, đọc spec_item của TẤT CẢ service liên quan, so từng cặp:

**1. Hợp đồng header giữa các service**
Gateway inject header gì, tên chính xác là gì. Mỗi service downstream đọc tên nào.
Lệch một chữ là hỏng. Ai đặt, ai xoá, ai tin.

**2. Giả định về token và phiên**
Thời hạn access/refresh, thuật toán ký, tên claim (`sub`, `tenant_id`,
`staff_id`...). Service A cấp một kiểu, service B giả định kiểu khác.

**3. Ranh giới tenant / seller**
Ai cấp `tenant_id` và `seller_id`, ai kiểm, ai chỉ tin. Service nào validate với
Identity, service nào chỉ kiểm định dạng. **Bất kỳ chỗ nào lệch nhau đều là
conflicts_with.**

**4. Phân quyền**
Identity có permission wildcard và anti-escalation rule. Service nào tôn trọng,
service nào tự làm luật riêng, service nào bỏ qua hoàn toàn.

**5. Giới hạn tần suất, timeout, kích thước**
Gateway đặt một mức, service sau giả định mức khác. Loại lỗi chỉ lộ khi tải cao
hoặc file lớn.

**6. Định dạng dữ liệu trao đổi**
Kiểu ngày giờ, đơn vị tiền, cách làm tròn, encoding, tên trạng thái đơn hàng.
OMS gọi trạng thái là gì, WMS hiểu là gì.

**7. CORS và cấu hình môi trường**
Mặc định khác nhau giữa dev và prod. Mặc định lọt lên prod là lỗ hổng.

## Cách làm

    spec_get(filter={"project_id": "..."})   cho từng project

Đọc theo cụm liên quan chứ đừng đọc hết một lượt:
  xác thực : voma-identity, voma-gateway, topvnsport-oms, topvnsport-pmi
  đơn hàng : topvnsport-oms, topvnsport-wms, voma-invoice
  hạ tầng  : voma-dev, topvnsport-devops, voma-gateway
  giao diện: topvnsport-web, voma-super-admin

## Yêu cầu chất lượng — nghiêm ngặt

**Mỗi `conflicts_with` PHẢI chỉ ra điểm mâu thuẫn cụ thể.** Không chỉ ra được thì
đó không phải conflict — đừng gán. Gán sai làm người ta đi sửa thứ không hỏng,
hại hơn không gán.

**Nghi mà chưa chắc: ĐỌC LẠI CODE để xác nhận trước khi gán.** Đường dẫn repo có
trong body của spec_item.

**Phân biệt dev với prod.** Đợt trước báo "X-Seller-Id là pass-through" mà không
phân biệt, đọc nguyên văn sẽ tưởng production đang thủng — thực ra prod xoá trắng.
Sai kiểu này gây báo động giả.

**Mục tiêu 30-50 `conflicts_with` mới.** Nhưng nếu đào kỹ mà hệ thống thật sự chỉ
có 15 điểm xung đột thì ghi 15. **Đừng nống số** — mỗi cái sai làm mất niềm tin
vào cả tập.

## Tuyệt đối không

- **Đừng tạo, sửa, xoá `spec_item` nào.** Chỉ nối quan hệ.
- **Đừng gán conflict vì hai spec cùng nhắc một từ khoá.**
- **Đừng sửa file nào trong repo.**

## Báo cáo cuối

1. Số `conflicts_with` mới, tách theo cặp service.
2. **Danh sách đầy đủ**, mỗi cái ghi rõ: mâu thuẫn ở ĐIỂM NÀO, và **hậu quả thực
   tế nếu không sửa**.
3. Xếp hạng theo mức nghiêm trọng. Cái nào là lỗ hổng bảo mật, cái nào chỉ là
   bất tiện.
4. Chỗ nào trong 7 mục trên bạn **không kiểm được** và vì sao.
5. Trả lời thẳng: hệ này thực sự có nhiều ràng buộc chéo hay ít? Nếu ít thì nói
   rõ — đó là thông tin có giá trị, không phải thất bại.
