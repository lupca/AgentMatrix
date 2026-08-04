# Gieo spec anchor

Khi ghi một code anchor bằng `spec_write`, gửi `repo`, `path`, `symbol` và
`relation`. Không gửi `anchor_sha`: server sẽ đọc source từ `projects.repo_root`
và tự tính hash của symbol bằng `compute_anchor_sha`.

Chỉ dùng `anchor_sha` thủ công khi repo không được checkout ở server. Khi đó
giá trị phải là hash symbol SHA-256 gồm đúng 64 ký tự hex; không dùng commit
SHA của `git rev-parse HEAD`.
