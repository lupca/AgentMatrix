Nối quan hệ chéo giữa các spec_item của VOMA — đợt 2.

## Bối cảnh

Đợt 1 đã rút xong 63 spec_item cho 3 project:

    voma-identity     21
    voma-gateway      23
    topvnsport-oms    19

Nhưng chúng đang **rời rạc**. Bảng `spec_relation` hiện **rỗng 0 dòng**.

Mục đích cuối của cả hệ spec sống là trả lời: *"thêm chức năng X có trùng hay
xung đột với cái gì không"*. Trong hệ 13 app, **xung đột nguy hiểm nhất nằm GIỮA
các service**, không nằm trong một service. Không nối được thì spec vẫn vô dụng
cho câu hỏi đó.

Đợt 1 tự nhận đúng lỗ hổng này:
> "Cross-service contracts — spec gateway nói inject 7 headers nhưng không liệt kê
> đầy đủ contract giữa OMS↔PIM↔WMS"

Đợt 2 là để vá chỗ đó.

## Việc của bạn

Đọc 63 spec_item đã có, tìm các cặp có quan hệ thật, ghi vào `spec_relation`
qua `spec_write`.

Bắt đầu bằng:

    spec_get(filter={"project_id": "voma-identity"})
    spec_get(filter={"project_id": "voma-gateway"})
    spec_get(filter={"project_id": "topvnsport-oms"})

## Bốn loại quan hệ — dùng đúng, đừng dùng bừa

    depends_on      A cần B mới hoạt động được.
                    VD: spec OMS về xác thực request  depends_on
                        spec identity về cấp JWT

    conflicts_with  A và B mâu thuẫn nhau — cả hai không thể cùng đúng.
                    ĐÂY LÀ LOẠI GIÁ TRỊ NHẤT. Tìm kỹ.
                    VD: gateway nói inject header X, OMS lại đọc header tên khác
                        identity nói token 15 phút, OMS giả định 60 phút

    duplicates      A và B nói cùng một điều ở hai nơi.
                    Nguy hiểm vì sửa một chỗ quên chỗ kia.

    refines         A là bản chi tiết hơn của B (thường xuyên project → app).

## Chỗ đáng đào nhất

Ba service này giao nhau ở đâu thì xung đột nằm ở đó:

1. **Xác thực**: identity cấp token → gateway kiểm → OMS tin.
   Ba bên có cùng giả định về thời hạn, thuật toán ký, tên claim không?

2. **Header giữa các service**: gateway "inject 7 headers". Bảy cái đó tên gì?
   OMS đọc đúng tên đó không? Đây là chỗ đợt 1 tự nhận bỏ sót.

3. **Định danh người dùng / seller**: identity gọi là gì, OMS scope theo
   `seller_id` — hai bên có cùng khái niệm không?

4. **Phân quyền**: identity có permission wildcard và anti-escalation rule.
   OMS/gateway có tôn trọng không, hay tự làm luật riêng?

5. **Giới hạn tần suất, timeout, retry**: gateway đặt một mức, OMS giả định mức
   khác thì lỗi chỉ lộ ra khi tải cao.

## Yêu cầu chất lượng

**Mỗi quan hệ phải dẫn được bằng chứng.** Không chỉ "hai cái này liên quan" —
phải nói rõ **liên quan thế nào** và **thấy ở đâu**.

**Với `conflicts_with` thì phải nói rõ mâu thuẫn ở điểm nào.** Nếu không chỉ ra
được điểm mâu thuẫn cụ thể thì đó không phải conflict — đừng gán.

**Nếu nghi có xung đột nhưng chưa chắc**: ĐỌC LẠI CODE để xác nhận trước khi gán.
Gán `conflicts_with` sai làm người ta đi sửa thứ không hỏng — hại hơn không gán.

**Số lượng: 15-40 quan hệ.** Ít mà chắc hơn nhiều mà đoán. Nếu ba service này
thật sự chỉ có 15 điểm giao thì ghi 15, đừng cố nống lên.

## Tuyệt đối không

- **Đừng gán quan hệ chỉ vì hai spec cùng nhắc một từ khoá.** Cùng chữ "token"
  không có nghĩa là có quan hệ.
- **Đừng sửa file nào trong repo.** Việc này CHỈ ĐỌC repo và GHI vào DB.
- **Đừng tạo thêm spec_item mới.** Đợt này chỉ nối cái đã có. Nếu phát hiện
  thiếu spec quan trọng thì GHI VÀO BÁO CÁO, đừng tự thêm.

## Báo cáo cuối

1. Số quan hệ đã tạo, tách theo từng `kind`.
2. **Danh sách đầy đủ mọi `conflicts_with`** kèm giải thích mâu thuẫn ở đâu.
   Đây là phần quan trọng nhất — người đọc sẽ hành động dựa trên nó.
3. Cặp service nào nhiều quan hệ nhất, cặp nào ít nhất. Ít bất thường có thể
   nghĩa là bạn chưa đào tới, không phải chúng độc lập.
4. Spec quan trọng nào bạn thấy **còn thiếu** để nối cho đủ.
5. Trả lời thẳng: **bây giờ đã đáp được câu "thêm chức năng X có xung đột không"
   chưa?** Nếu chưa thì còn thiếu gì cụ thể?
