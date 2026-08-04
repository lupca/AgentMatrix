# CTV2-1375 — anchor repair report

## Decision

Nhóm A chọn hướng (a): hash toàn bộ file cho mọi anchor không phải Python.
Lý do: các định dạng này không có một block symbol ổn định trong cơ chế hiện
tại; whole-file hash là deterministic và không cho phép thay đổi cấu hình mà
không bị phát hiện. `anchor_mode(path)` dùng cùng quy tắc khi ghi anchor và khi
`apply_commit_staleness` kiểm tra commit, không cần migration hay cột kiểu mới.
Đổi lại, mọi thay đổi trong file cấu hình sẽ làm anchor stale.

## Phân loại dữ liệu ban đầu

Truy vấn ban đầu cho thấy 231/862 `anchor_sha` là SHA commit 40 ký tự. 169
anchor Nhóm A đều có file tồn tại trong `projects.repo_root`:

| Đuôi | Số lượng | Cách xử lý |
|---|---:|---|
| `.tf` | 69 | whole-file hash |
| `.conf` | 46 | whole-file hash |
| `.yml` | 21 | whole-file hash |
| `.sh` | 15 | whole-file hash |
| `.md` | 10 | whole-file hash |
| `.toml` | 2 | whole-file hash |
| không đuôi / `.example` / `.sql` / `.tsx` | 6 | whole-file hash |
| **Tổng Nhóm A** | **169** | |

Nhóm B có 62 file `.py`, được xử lý riêng:

- **39 anchor là declaration cục bộ** và được tính lại bằng Python AST. Các
  dạng đã xác minh gồm module assignment/annotated assignment, class
  attribute (`Role.permissions`, `Seller.tenant_id`, `StaffCreate.password`),
  và các declaration bị regex cũ bỏ sót như `TOOL_REGISTRY`, `MODES`,
  `ALLOWED_TRANSITIONS`.
- **7 anchor là import/external reference**, không phải symbol do file sở
  hữu, nên không thể có source block hợp lệ để neo: `FastMCP`,
  `CORSMiddleware` (2), `RequestContextMiddleware`, `SUPPORTED_CLIS`,
  `get_bulk_computed_prices`, `get_variant_computed_price`.
- **16 anchor không phải local declaration** và bị xóa: tên biến bị cắt/sai
  (`MAX_COST_USD`, `MAX_TOKENS`, `CORS_ALLOWED_ORIGINS`), tên constraint
  (`ck_*`), tên keyword/call-site (`manage_inbox`,
  `CORSMiddleware.allow_origins`), chuỗi nhiều class (`Tenant,Seller,...`),
  environment lookup (`FERNET_KEY`, `PUBLIC_SELLER_ID`,
  `PUBLIC_TENANT_ID`) và runtime reference (`secrets.compare_digest`).

Nguyên nhân gốc của Nhóm B là `extract_symbol_source` cũ chỉ nhận dòng khớp
regex `def/class/function/interface/type/const/let/var`; nó không nhận Python
assignment, annotated assignment, class attribute, import, constraint name,
hoặc reference đến symbol ở module khác. Vì vậy không phải lỗi `repo_root`.
Sau thay đổi, Python chỉ nhận declaration cục bộ; anchor không xác định được
không còn được giữ bằng hash thủ công.

## Kết quả sau repair

Script `scripts/recalculate_spec_anchors.py` cập nhật cả anchor cũ 64 ký tự
để chuyển Nhóm A sang whole-file hash, cập nhật 39 Python declaration, và xóa
23 Python anchor không hợp lệ. Không có migration được thêm.

Truy vấn kiểm chứng bắt buộc sau khi chạy repair đã trả về:

```sql
SELECT count(*) FILTER (WHERE length(anchor_sha)=64) AS dung,
       count(*) FILTER (WHERE length(anchor_sha)<>64) AS con_sai,
       count(*) AS tong
FROM spec_anchor;
```

`dung=839`, `con_sai=0`, `tong=839`.
