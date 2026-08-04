# Gieo spec anchor

Khi ghi một anchor bằng `spec_write`, gửi `repo`, `path`, `symbol` và
`relation`. Không gửi `anchor_sha`: server sẽ đọc source từ `projects.repo_root`
và tự tính hash theo cơ chế của loại file.

Với file Python, `symbol` phải là khai báo cục bộ thật: function, class, biến,
annotated assignment, hoặc thuộc tính trực tiếp của class (`Class.member`).
Không neo tên import, tên constraint, keyword, hay symbol được gọi từ module
khác.

Với file cấu hình và mọi file không phải Python, cơ chế dùng hash của toàn bộ
file. `symbol` chỉ là nhãn mô tả vị trí/ý nghĩa của anchor; thay đổi bất kỳ
dòng nào trong file sẽ làm spec stale. Không dùng `git rev-parse HEAD` làm
`anchor_sha`.

**TUYỆT ĐỐI KHÔNG tự điền `anchor_sha`.** Kể cả khi repo không checkout được ở
server — khi đó hãy BỎ QUA anchor đó và báo lại, đừng đoán giá trị.

Lý do (sự cố 2026-08-04): một prompt trước đây bảo agent điền
`git rev-parse HEAD`. 283/862 anchor lưu commit SHA 40 ký tự thay vì hash nội
dung 64 ký tự. Hai thứ này không bao giờ khớp nhau, nên lần commit kế tiếp vào
các repo đó sẽ đánh dấu stale hàng loạt — cảnh báo giả che mất cảnh báo thật.
CTV2-1375 phải dọn: sửa 39 anchor, xoá 23 anchor không cứu được.

Anchor thiếu thì chỉ mất một liên kết. Anchor sai thì phá cả cơ chế phát hiện lệch.
