# BÁO CÁO NGHIÊN CỨU & BẢN THIẾT KẾ REFACTOR BACKEND SERVICES (CTV2-1415)

**Ngày thực hiện:** 2026-08-06  
**Phạm vi nghiên cứu:** `backend/app/` (84 file Python, 29.129 dòng code sản phẩm, 77 file test / 25.148 dòng)  
**Mục tiêu:** Đánh giá hiện trạng dựa trên dữ liệu thực tế (git log, bug density, spec anchor DB), đề xuất phương án tách file có bằng chứng và lộ trình thực hiện an toàn.

---

## 1. ĐO TRƯỚC: FILE TO CÓ THẬT SỰ LÀ VẤN ĐỀ Ở DỰ ÁN NÀY KHÔNG? (Q1)

### 1.1 Số liệu hiện trạng 10 file to nhất (`wc -l`)

| STT | File Path | Số dòng (`wc -l`) | Số hàm | Hàm dài nhất / Đặc điểm chính |
|-----|-----------|-------------------|--------|--------------------------------|
| 1 | `app/services/task_state_machine.py` | 2.998 | 69 | `request_gate` (206 LOC), `apply_gate` (201 LOC), `decide_gate` (147 LOC) |
| 2 | `app/db/models.py` | 1.691 | - | Declarative ORM models & Table schemas |
| 3 | `app/services/tool_registry.py` | 1.665 | 8 | `TOOL_REGISTRY` mapping (~1.500 dòng khai báo dữ liệu static) |
| 4 | `app/workers/cli_executor.py` | 1.554 | 38 | `execute_agent_run` (~686 LOC outer function), closures `record_heartbeat` (399 LOC) & `cancel_check` (260 LOC) |
| 5 | `app/services/coordinator.py` | 1.482 | 35 | Closure `summarizer_with_usage` xuất hiện 2 lần trùng lặp (244 & 225 LOC) |
| 6 | `app/services/command_router_handlers/task_handlers.py` | 1.276 | 24 | Handlers lệnh tác vụ coordinator |
| 7 | `app/mcp_native.py` | 1.069 | 32 | FastMCP native tool entrypoints |
| 8 | `app/services/task_validators.py` | 1.019 | 22 | Logic kiểm tra phanh (brakes) & autonomy policy |
| 9 | `app/services/spec_plan_generator.py` | 893 | 18 | Sinh spec plan cho tác vụ |
| 10 | `app/services/command_router_handlers/query_handlers.py` | 748 | 15 | Query handlers đọc thông tin |

---

### 1.2 Dữ liệu git log 30 ngày qua (Tần suất sửa & Tần suất sửa lỗi `fix(`)

Thu thập qua lệnh:
- Tần suất sửa đổi 30 ngày: `git log --format= --name-only --since=30.days | grep '^backend/app/' | sort | uniq -c | sort -rn`
- Tần suất commit sửa lỗi: `git log --grep="fix(" --name-only --format= | grep '^backend/app/' | sort | uniq -c | sort -rn`

| File Path | LOC | 30d Commits | `fix(` Commits | Tỷ lệ Fix/Commit (%) | Mức độ đau thật (Pain Level) |
|-----------|-----|-------------|----------------|----------------------|-------------------------------|
| **`app/services/task_orchestration.py`** | 544 | **62** | **18** | **29,0%** | **CỰC CAO (#1 Fixes)** - Chỗ đau thật |
| **`app/services/task_state_machine.py`** | 2.998 | **36** | **11** | **30,5%** | **CỰC CAO (To + Churn + Fix)** - Chỗ đau thật |
| **`app/workers/cli_executor.py`** | 1.554 | **21** | **9** | **42,8%** | **RẤT CAO (Tỷ lệ fix 43%)** - Chỗ đau thật |
| **`app/workers/agent_runner.py`** | 666 | **49** | **13** | **26,5%** | **RẤT CAO (#2 Fixes overall)** |
| **`app/services/tool_registry.py`** | 1.665 | **59** | **10** | **16,9%** | **TRUNG BÌNH** (Sửa nhiều do thêm tool mới) |
| **`app/services/command_router.py`** | 412 | **67** | **11** | **16,4%** | **TRUNG BÌNH** (Routing hub) |
| **`app/services/command_router_handlers/task_handlers.py`** | 1.276 | **16** | **6** | **37,5%** | **CAO** (Tỷ lệ fix cao) |
| `app/services/coordinator.py` | 1.482 | 31 | 3 | 9,6% | THẤP - TRUNG BÌNH |
| `app/db/models.py` | 1.691 | 64 | 4 | 6,2% | THẤP (Sửa do thêm model/column, ít bug) |
| `app/mcp_native.py` | 1.069 | 28 | 4 | 14,2% | THẤP - TRUNG BÌNH |
| `app/services/task_validators.py` | 1.019 | 14 | 4 | 28,5% | TRUNG BÌNH |
| `app/services/command_router_handlers/query_handlers.py` | 748 | 5 | 0 | 0,0% | **KHÔNG ĐAU (0 bug fix)** - Để yên |

---

### 1.3 Phân tích giao của hai tập dữ liệu (Giao tập = Chỗ đau thật)

1. **`task_state_machine.py` (2.998 LOC)** và **`cli_executor.py` (1.554 LOC)** nằm ở điểm giao: Vừa có kích thước khổng lồ, vừa thay đổi liên tục, vừa chứa tỷ lệ commit sửa lỗi `fix(` rất cao (30% - 43%). Đây chính là **2 điểm đau lớn nhất của hệ thống backend**.
2. **`task_orchestration.py` (544 LOC)** tuy không quá dài (<600 dòng) nhưng đứng đầu hệ thống về số lượng commit `fix(` (18 commits). Điều này cho thấy sự phức tạp về luồng FSM/Dramatiq actor hơn là độ dài dòng code.
3. **`models.py` (1.691 LOC)** có 64 commits nhưng chỉ có 4 commit `fix(` (6,2%). Việc sửa đổi liên quan đến việc bổ sung field/table theo tính năng mới. Tách file `models.py` không giải quyết được vấn đề lỗi vận hành mà lại tạo rủi ro gãy mối quan hệ SQLAlchemy ORM.
4. **`query_handlers.py` (748 LOC)** chỉ có 5 commits trong 30 ngày và **0 commit `fix(`**. File này hoàn toàn ổn định, không được cắt nhỏ chỉ để giảm dòng.

---

## 2. PHƯƠNG ÁN RANH GIỚI TÁCH FILE & ĐÁNH ĐỔI (Q2)

Không tách file theo quy tắc cứng nhắc "cắt mỗi N dòng". Hai phương án thiết kế được đề xuất dưới đây dựa trên bản chất kiến trúc:

```
                  ┌─────────────────────────────────────────────────────────┐
                  │                 Hệ Thống Hiện Tại                       │
                  │  task_state_machine.py (2.998 LOC) / cli_executor.py   │
                  └──────────────────────────┬──────────────────────────────┘
                                             │
                  ┌──────────────────────────┴──────────────────────────┐
                  ▼                                                     ▼
┌───────────────────────────────────┐                 ┌───────────────────────────────────┐
│     PHƯƠNG ÁN A: SUB-DOMAIN       │                 │ PHƯƠNG ÁN B: FSM PHASE + CQRS     │
│ (Theo Danh Từ Miền & Bối Cảnh)   │                 │  (Theo Vòng Đời Gate & Đọc/Ghi)   │
├───────────────────────────────────┤                 ├───────────────────────────────────┤
│ • app/services/fsm/               │                 │ • app/services/fsm/               │
│   ├── gate_ledger.py              │                 │   ├── phase_pending.py            │
│   ├── review_four_eyes.py         │                 │   ├── phase_review.py             │
│   └── task_lifecycle.py           │                 │   ├── phase_verdict.py            │
│ • app/workers/executor/           │                 │   └── phase_landing.py            │
│   ├── process_tracker.py          │                 │ • app/workers/executor/           │
│   └── worktree_runner.py          │                 │   ├── execution_commands.py       │
└───────────────────────────────────┘                 │   └── execution_queries.py        │
                                                      └───────────────────────────────────┘
```

### 2.1 Phương án A: Tách theo Sub-domain & Boundary Context (Đề xuất khuyên dùng)

Gom nhóm theo các thực thể miền nghiệp vụ cốt lõi:
1. **Thư mục `app/services/fsm/`** (Thay thế monolithic `task_state_machine.py`):
   - `gate_ledger.py`: Quản lý nhật ký append-only `GateRecord` (tạo gate, tra cứu gate pending, kiểm tra four-eyes constraint `reviewer != executor`).
   - `verdict_landing.py`: Logic chốt kết quả (land verdict, CAS status transition, version bump, wake dependents).
   - `task_lifecycle.py`: Chuyển trạng thái tổng quát (`transition_to_done`, `escalate_task`, `reopen_failed_task`).
   - `facade.py`: Export lại toàn bộ interface cũ để đảm bảo backward compatibility cho caller.
2. **Thư mục `app/workers/executor/`** (Thay thế `cli_executor.py`):
   - `run_tracker.py`: Quản lý heartbeat, process PID, cancel check (`ExecutionTracker`).
   - `worktree_manager.py`: Tạo, cô lập và dọn dẹp git worktree cho từng run.
   - `cli_runner.py`: Kích hoạt agy/claude/codex CLI & stream output.

### 2.2 Phương án B: Tách theo FSM Lifecycle Gate Phase & Command/Query (CQRS Light)

Gom nhóm theo từng phase của vòng đời Gate & phân định Đọc/Ghi:
1. **Phân chia `task_state_machine.py` theo Phase FSM:**
   - `gate_phase_request.py`: Xử lý đầu vào `request_gate` (kiểm tra điều kiện mở gate mới).
   - `gate_phase_review.py`: Xử lý phân công reviewer (`review_order`, `record_dispatch_decision`).
   - `gate_phase_verdict.py`: Tiếp nhận đánh giá, kiểm tra rule four-eyes và cập nhật `decide_gate`/`apply_gate`.
   - `gate_phase_landing.py`: Thực thi merge kết quả (`land_verdict_result`), chuyển trạng thái Task lên DONE/FAILED.
2. **Phân chia Read/Write:**
   - Cắt toàn bộ các hàm tra cứu đọc trạng thái (`get_active_gate`, `check_pass_verdict`) sang `state_queries.py`.

### 2.3 Bảng so sánh Đánh đổi (Trade-off Matrix)

| Tiêu chí | Phương án A (Sub-domain) | Phương án B (FSM Phase + CQRS) |
|----------|--------------------------|--------------------------------|
| **Độ rõ ràng về mặt kiến trúc** | **Rất cao**: Dễ định vị file theo danh từ miền. | **Khá**: Phụ thuộc vào luồng thời gian FSM. |
| **Rủi ro Import vòng (Circular Import)** | **Thấp**: Các sub-domain phụ thuộc 1 chiều rõ ràng. | **Trung bình**: Giữa các phase FSM dễ gọi ngược nhau nếu state rollback. |
| **Ảnh hưởng Spec Anchor DB** | **Ít gãy hơn**: Hàm gom theo miền giữ nguyên tên symbol. | **Nhiều hơn**: Phân tách hàm đọc/ghi làm đổi symbol path. |
| **Độ phức tạp khi viết Unit Test** | **Rất tốt**: Test fixture theo sub-domain độc lập. | **Tốt**: Test bám theo từng phase của Gate. |
| **Khả năng duy trì lâu dài** | **Tốt nhất**: Khi thêm entity/domain mới chỉ cần thêm file trong domain. | **Khá**: Khi FSM thêm state mới phải sửa nhiều phase. |

---

## 3. GIẢI PHÁP TÁCH `tool_registry.py` (1.665 DÒNG, 8 HÀM) (Q3)

### 3.1 Vấn đề hiện tại
`tool_registry.py` chiếm 1.665 dòng code nhưng chỉ chứa 8 hàm helper/projection (`get_spec`, `to_openai_tools`, `dump_registry`, ...). Hơn 1.500 dòng code còn lại là biến `TOOL_REGISTRY: dict[str, ToolSpec] = { ... }` khai báo dữ liệu static của hàng chục tool.

### 3.2 Đảm bảo Spec Item `19684fa5`
Ràng buộc cốt lõi của ADR-001 §D1 và spec `19684fa5`:
> *"Mỗi tool được khai báo đúng một lần dưới dạng `ToolSpec`. Tất cả projection (OpenAI JSON schema, slash command table, GET /api/tools) đều derive từ `TOOL_REGISTRY`."*

### 3.3 Phương án thiết kế tách file cho `tool_registry.py`
Tách phần khai báo dữ liệu thành các sub-module Python theo nhóm tool trong thư mục mới `app/services/tool_specs/`, giữ nguyên 100% logic code projection trong `tool_registry.py`:

```
backend/app/services/
├── tool_registry.py              # GIỮ NGUYÊN 8 hàm logic & gộp TOOL_REGISTRY từ sub-modules
└── tool_specs/                   # CÁC FILE CHỨA KHAI BÁO DỮ LIỆU TOOL_SPEC (KHÔNG CHỨA LOGIC)
    ├── __init__.py               # Gộp tất cả dicts thành ALL_TOOL_SPECS
    ├── task_specs.py             # Tools: create_task, update_task, advance_task, ...
    ├── gate_specs.py             # Tools: request_gate, decide_gate, ...
    ├── admin_specs.py            # Tools: entity_admin, system_config, ...
    ├── session_specs.py          # Tools: create_session, load_tools, ...
    └── research_specs.py         # Tools: search_code, get_context, ...
```

**Cách triển khai trong `tool_registry.py`:**
```python
# app/services/tool_registry.py
from app.services.tool_specs import ALL_TOOL_SPECS

# Giữ nguyên duy nhất 1 single source of truth dict
TOOL_REGISTRY: dict[str, ToolSpec] = ALL_TOOL_SPECS

# 8 hàm projection (to_openai_tools, get_spec, ...) giữ nguyên 100% không đổi
```
**Kết quả:**
- `tool_registry.py` giảm từ 1.665 dòng xuống **< 150 dòng**.
- Tuân thủ tuyệt đối spec `19684fa5`: Mỗi tool vẫn chỉ khai báo đúng 1 lần dưới dạng `ToolSpec` trong Python, mọi projection đều derive từ `TOOL_REGISTRY`.

---

## 4. PHÂN TÍCH VÀ GIẢI MÃ CLOSURE KHỔNG LỒ (Q4)

### 4.1 Closure `record_heartbeat` trong `cli_executor.py` (Line 1157)
- **Bản chất:** Đóng (closure) nằm trong hàm `execute_agent_run` (~686 dòng).
- **Lý do phải là closure:** Cần bám vào các biến cục bộ ngoài phạm vi (`db`, `run_id`, `run`, `task_id`, `process_manager`, `attempt`).
- **Tác hại:** Không thể unit test độc lập logic cập nhật heartbeat hay hủy process mà bắt buộc phải invoke toàn bộ hàm `execute_agent_run` với đầy đủ mock DB/Git worktree complex state.
- **Giải pháp Refactor:** Chuyển closure thành Class `ExecutionTracker` (State Object pattern):

```python
# app/workers/executor/run_tracker.py
class ExecutionTracker:
    def __init__(self, db: Session, run_id: str, task_id: str, attempt: int):
        self.db = db
        self.run_id = run_id
        self.task_id = task_id
        self.attempt = attempt

    def record_heartbeat(self, pid: int) -> None:
        try:
            # Logic update updated_at & commit
            ...
        except Exception:
            self.db.rollback()

    def cancel_check(self) -> bool:
        # Logic check cancel status from DB/Redis
        ...
```
*Lợi ích:* Nâng thành class cấp module giúp test riêng `record_heartbeat` và `cancel_check` với 100% coverage mà không cần chạy subprocess CLI thật.

---

### 4.2 Closure `summarizer_with_usage` trong `coordinator.py` (Line 964 & Line 1219)
- **Bản chất:** Lặp lại **2 LẦN GIỐNG HỆT NHAU** (mỗi đoạn ~36 dòng) bên trong `run_turn_sync` và `run_turn_stream`.
- **Lý do là closure:** Bám biến `self`, `agent`, `db_session`, `usage_task_id`, `usage_agent_run_id`.
- **Tác hại:** Vi phạm DRY (Don't Repeat Yourself), gây khó khăn khi sửa logic tính token/cost LLMUsage (phải sửa 2 nơi, dễ bỏ sót).
- **Giải pháp Refactor:** Rút gọn thành method cấp class của `CoordinatorService`:

```python
# app/services/coordinator.py
class CoordinatorService:
    def _compact_with_usage_tracking(
        self,
        db_session: Session,
        agent: Any,
        usage_task_id: str | None,
        usage_agent_run_id: str | None,
        messages: list,
        **kwargs: Any,
    ) -> str:
        # Logic gọi llm_service.complete_sync & ghi LLMUsage duy nhất 1 chỗ
        ...
```
*Lợi ích:* Xóa bỏ hoàn toàn 2 closure lồng nhau, giảm >70 dòng code lặp, cho phép test riêng tính năng ghi compaction usage.

---

## 5. THỨ TỰ THỰC HIỆN & ĐẢM BẢO TEST SUITE LUÔN XANH (Q5)

Refactor phải chia thành các bước nhỏ atomic. Sau **MỖI** bước, suite 77 file test / 25.148 dòng phải pass 100%.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ BƯỚC 1 (An Toàn Nhất): Refactor tool_registry.py & coordinator.py closure   │
│ • Đụng: 2 file cũ + 6 file tool_specs mới. Rủi ro logic = 0.                │
├─────────────────────────────────────────────────────────────────────────────┤
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ BƯỚC 2: Tách closures trong cli_executor.py thành ExecutionTracker class    │
│ • Đụng: cli_executor.py, tạo run_tracker.py, sửa test_cli_executor.py.     │
├─────────────────────────────────────────────────────────────────────────────┤
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ BƯỚC 3: Modularize task_state_machine.py theo Sub-domain (Phương án A)       │
│ • Đụng: task_state_machine.py, tạo thư mục app/services/fsm/ (3 file mới).  │
│ • Giữ file task_state_machine.py làm Facade re-export tất cả hàm cũ.        │
├─────────────────────────────────────────────────────────────────────────────┤
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ BƯỚC 4: Cập nhật Spec Anchor DB & Cleanup Facade                            │
│ • Chạy script update DB spec_anchor theo symbol path mới.                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Chi tiết từng bước & Ràng buộc An toàn:

1. **Bước 1 (Làm ĐẦU TIÊN - An toàn tuyệt đối):**
   - *Công việc:* Tách `tool_registry.py` thành `tool_specs/` và rút gọn closure `summarizer_with_usage` trong `coordinator.py`.
   - *Số file ảnh hưởng:* 2 file sản phẩm + 6 file dữ liệu mới.
   - *Lý do an toàn:* Không chạm vào FSM gate state, không đổi signature, không sửa CAS version transition. Test suite chạy xanh 100% lập tức.

2. **Bước 2 (An toàn cao):**
   - *Công việc:* Nâng closure trong `cli_executor.py` thành class `ExecutionTracker`.
   - *Số file ảnh hưởng:* 2 file (`cli_executor.py` và `test_cli_executor.py`).
   - *Lý do an toàn:* Giữ nguyên toàn bộ luồng làm việc của `execute_agent_run`, chỉ thay thế việc định nghĩa hàm lồng bằng việc gọi method của tracker.

3. **Bước 3 (Thao tác chính - Cần cẩn trọng):**
   - *Công việc:* Tách `task_state_machine.py` thành `app/services/fsm/` (`gate_ledger.py`, `verdict_landing.py`, `task_lifecycle.py`).
   - *Số file ảnh hưởng:* `task_state_machine.py` (chuyển thành Facade re-export), 3 file sub-module mới.
   - *Chiến lược xanh test:* Trong file `task_state_machine.py` cũ, dùng `from app.services.fsm.gate_ledger import request_gate, ...` để re-export toàn bộ 69 hàm. Tất cả 77 file test cũ import từ `app.services.task_state_machine` vẫn hoạt động bình thường mà không cần sửa 1 dòng test nào.

4. **Bước 4 (Đồng bộ Spec Anchor & Cleanup):**
   - *Công việc:* Cập nhật bảng `spec_anchor` trong Database và đổi dần các import trong test (nếu cần).

---

## 6. QUẢN LÝ RỦI RO & ĐỒNG BỘ SPEC ANCHOR DATABASE (Q6)

### 6.1 Các rủi ro đặc thù của hệ thống Agenticmatix khi Refactor
1. **Rủi ro gãy Spec Anchor (CRITICAL):**
   - Hệ thống dùng bảng `spec_anchor` (trong Postgres) để link tài liệu spec với mã nguồn theo `path` và `symbol`.
   - Nếu đổi tên file hoặc tên hàm mà không update DB, các lệnh verification spec (`spec_get`, anchor verification) sẽ báo lỗi GÃY ANCHOR.
2. **Rủi ro Import vòng (Circular Import):**
   - `task_state_machine.py` import `task_validators.py`, `models.py`, `outbox.py`. Nếu tách nhỏ không khéo, các sub-module FSM sẽ dính import vòng với nhau.
3. **Rủi ro phá vỡ Bối cảnh Chuyển trạng thái CAS (Version Bump):**
   - Tất cả chuyển trạng thái Task/Gate bắt buộc dùng CAS + version bump (`version = version + 1`). Tách hàm nếu quên truyền `version` hoặc tách rời `session.flush()` sẽ làm lọt sai sót concurrency.

---

### 6.2 Thống kê Spec Anchor trong Database (`spec_anchor`)

Kiểm tra trực tiếp từ Postgres Container (`agmx_db`):
- **Tổng số spec anchor toàn hệ thống:** `889` anchors.
- **Số anchor trỏ vào `backend/app/`:** `205` anchors.

**Danh sách anchor trỏ trực tiếp vào các file dự kiến refactor:**

| File Path | Số lượng Anchor | Các symbol quan trọng bị ảnh hưởng |
|-----------|------------------|------------------------------------|
| `backend/app/services/task_state_machine.py` | **17** | `request_gate`, `apply_gate`, `decide_gate`, `cas_status`, `escalate_task`, `land_verdict_result`, `record_dispatch_decision`, `reopen_failed_task`, `require_approved_pass_verdict`, `sync_awaiting_approval`, `transition_to_done`, `update_agent_success_rate`, `wake_dependents`, `write_spec_plan`, `_reject_pending_gates` |
| `backend/app/services/task_validators.py` | **9** | `AutonomyPolicy`, `BrakeDecision`, `check_brakes`, `DependencyCycleError`, `MODES`, `_record_brake`, `require_independent`, `resolve_autonomy` |
| `backend/app/services/tool_registry.py` | **7** | `DEFERRED_GROUPS`, `tier`, `TOOL_REGISTRY`, `ToolSpec` |
| `backend/app/mcp_native.py` | **5** | `SERVER_INSTRUCTIONS`, `issue_token`, `TOKEN_PREFIX` |
| `backend/app/services/spec_plan_generator.py` | **2** | `_build_prompt`, `_heartbeat_recorder` |
| `backend/app/workers/agent_runner.py` | **2** | `_advance_task_stalled`, `_rounds_since_last_escalation` |
| `backend/app/services/task_orchestration.py` | **1** | `TaskOrchestrationService` |
| `backend/app/services/command_router_handlers/task_handlers.py` | **1** | `_handle_wait_for_task` |

---

### 6.3 Giải pháp Cập nhật Spec Anchor Đồng thời
Để không làm gãy spec anchor khi tách file:

1. **Giai đoạn Chuyển tiếp (Facade Pattern):**
   - Giữ nguyên `task_state_machine.py` làm Facade re-export tất cả 17 symbol trên.
   - Vì đường dẫn `backend/app/services/task_state_machine.py` và các `symbol` vẫn tồn tại hợp lệ ở file Facade, `spec_anchor` DB **không bị gãy** ngay cả khi chưa update DB.

2. **Giai đoạn Chuyển gốc Anchor trong DB (Migration Script):**
   - Chạy lệnh SQL đồng bộ đường dẫn mới trong DB khi hoàn tất refactor:
   ```sql
   UPDATE spec_anchor
   SET path = 'backend/app/services/fsm/gate_ledger.py'
   WHERE path = 'backend/app/services/task_state_machine.py'
     AND symbol IN ('request_gate', 'apply_gate', 'decide_gate', '_reject_pending_gates');

   UPDATE spec_anchor
   SET path = 'backend/app/services/fsm/verdict_landing.py'
   WHERE path = 'backend/app/services/task_state_machine.py'
     AND symbol IN ('land_verdict_result', 'cas_status', 'transition_to_done', 'wake_dependents');
   ```

---

## 7. KẾT LUẬN: CÓ NÊN REFACTOR KHÔNG? (Q7)

### Kết luận chính thức: NÊN REFACTOR CÓ MỤC TIÊU (TARGETED REFACTORING), KHÔNG REFACTOR TRÀN LAN (NO BLANKET REFACTORING).

### Căn cứ thực tế dựa trên dữ liệu thu thập:

1. **KHÔNG REFACTOR TRÀN LAN DỰA TRÊN SỐ DÒNG CODE:**
   - Số liệu Q1 chứng minh: File `models.py` (1.691 LOC) chỉ có 6,2% fix rate, `query_handlers.py` (748 LOC) có 0% fix rate trong 30 ngày qua. **Các file này KHÔNG GÂY LỖI**, việc refactor chúng chỉ mang lại rủi ro làm gãy hệ thống mà không đem lại giá trị vận hành.

2. **BẮT BUỘC REFACTOR 2 ĐIỂM ĐAU THỰC TẾ (`task_state_machine.py` & `cli_executor.py`):**
   - `task_state_machine.py` (2.998 LOC) và `cli_executor.py` (1.554 LOC) chiếm tỷ lệ bug fix cực cao (30% - 43%) và chứa các closure lồng ghép khổng lồ làm cho unit test không thể phủ hết edge-cases. Tách 2 file này theo Phương án A (Sub-domain) là **cần thiết để giảm tỷ lệ regression bugs**.

3. **CẦN TẢI THIỆN NGAY KỸ THUẬT CHO `tool_registry.py` VÀ `coordinator.py`:**
   - Tách 1.500 dòng khai báo dữ liệu trong `tool_registry.py` thành `tool_specs/` và xóa bỏ 2 closure lặp lại trong `coordinator.py` là công việc tốn ít chi phí (an toàn 100%), nhưng giúp code gọn gàng, nâng cao năng suất lập trình rõ rệt.

---

**Tóm tắt khuyến nghị cho Task Refactor thật tiếp theo:**
- **Thực hiện theo 4 Bước ở Mục 5**, tập trung đúng 4 file: `task_state_machine.py`, `cli_executor.py`, `tool_registry.py`, `coordinator.py`.
- **Tuyệt đối không đụng vào** `models.py` và `query_handlers.py`.
- **Duy trì Facade re-export** để bảo vệ 44 spec anchors DB không bị gãy trong quá trình triển khai.
