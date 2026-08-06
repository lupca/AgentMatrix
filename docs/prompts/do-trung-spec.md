Dò spec trùng lặp và mâu thuẫn trên toàn bộ 218 spec_item của VOMA.

## Bối cảnh

11 project VOMA vừa được rút spec xong, tổng **218 spec_item**:

    voma-dev 25 · voma-gateway 23 · voma-super-admin 21 · voma-identity 21
    topvnsport-devops 20 · topvnsport-web 20 · topvnsport-oms 19
    voma-invoice 18 · topvnsport-pmi 18 · topvnsport-wms 17
    voma-agentic-test 15 · voma-qa-agent 1

Chúng được rút **độc lập, song song**, mỗi project một agent riêng không nhìn thấy
nhau. Nên chắc chắn có trùng lặp và có thể có mâu thuẫn.

`spec_relation` hiện có 18 dòng (12 `depends_on`, 6 `conflicts_with`) — chỉ mới nối
giữa identity/gateway/oms. Chín project còn lại chưa nối gì.

## Nghi vấn đã biết, kiểm trước

**`voma-dev` gần như chắc chắn trùng nhiều.** Nó là repo docker-compose điều phối
toàn hệ, nên agent đọc xuyên qua mọi service. Spec của nó mô tả OMS, Identity,
Gateway, PIM — tức **nói lại điều mà spec của chính các project đó đã nói**.

Ví dụ có thật, `voma-dev` có mệnh đề:

    "Identity cấp JWT HS256 access token 15 phút, refresh token ngẫu nhiên 7 ngày"

Trong khi `voma-identity` gần như chắc chắn có mệnh đề tương đương. Đó là
`duplicates`.

## Việc của bạn

Đọc toàn bộ 218 spec_item, tìm các cặp có quan hệ, ghi vào `spec_relation` qua
`spec_write`.

Đọc theo project để đỡ tràn ngữ cảnh:

    spec_get(filter={"project_id": "..."})

## Bốn loại quan hệ

    duplicates      A và B nói CÙNG một điều ở hai nơi.
                    Nguy hiểm vì sửa một chỗ quên chỗ kia.
                    ƯU TIÊN CAO NHẤT đợt này.

    conflicts_with  A và B mâu thuẫn — cả hai không thể cùng đúng.
                    Phải chỉ rõ mâu thuẫn ở ĐIỂM NÀO.

    depends_on      A cần B mới hoạt động được.

    refines         A là bản chi tiết hơn của B.

## Yêu cầu chất lượng

**`duplicates` phải thật sự nói cùng một điều**, không phải "cùng chủ đề".
Hai spec cùng nói về JWT nhưng một cái nói thời hạn, một cái nói thuật toán ký —
đó KHÔNG phải duplicate.

**`conflicts_with` phải chỉ ra điểm mâu thuẫn cụ thể.** Không chỉ ra được thì
đừng gán. Gán sai làm người ta đi sửa thứ không hỏng.

**Nếu nghi mâu thuẫn nhưng chưa chắc: ĐỌC LẠI CODE để xác nhận.**

**Số lượng: 30-80 quan hệ.** 218 spec rút độc lập từ 11 project thì lượng trùng
sẽ đáng kể. Nhưng đừng nống số — ít mà chắc hơn nhiều mà đoán.

## Tuyệt đối không

- **Đừng tạo, sửa, hay xoá `spec_item` nào.** Đợt này CHỈ nối quan hệ.
- **Đừng gán quan hệ chỉ vì hai spec cùng nhắc một từ khoá.**
- **Đừng sửa file nào trong repo.**

## Báo cáo cuối

1. Số quan hệ đã tạo, tách theo `kind`. **Kiểm lại phép cộng.**
2. **Danh sách đầy đủ `conflicts_with`** kèm giải thích mâu thuẫn ở đâu.
3. **Cụm trùng lặp lớn nhất**: nhóm spec_item nào nói đi nói lại cùng một điều
   ở nhiều project nhất? Đó là chỗ nên gộp.
4. Project nào trùng nhiều nhất với project khác. Nếu `voma-dev` trùng quá nhiều
   thì nêu rõ: có nên giữ spec riêng cho nó, hay chỉ giữ quan hệ trỏ sang project gốc?
5. Trả lời thẳng: với 218 spec + quan hệ vừa nối, **bây giờ đã đáp được câu
   "thêm chức năng X có trùng/xung đột không" chưa?** Chưa thì còn thiếu gì cụ thể?
