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

Chỉ dùng `anchor_sha` thủ công khi repo không được checkout ở server. Giá trị
phải là SHA-256 64 ký tự hex của đúng nội dung theo các quy tắc trên.
