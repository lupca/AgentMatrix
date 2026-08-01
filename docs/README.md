# docs/ — bản đồ tài liệu

Đọc theo thứ tự này nếu mới vào dự án (hoặc là AI agent cần nạp lại ngữ cảnh):

1. **`spec/`** — đặc tả hệ thống, LUÔN phản ánh hiện trạng code (cập nhật khi đổi hành vi):
   - `01-overview.md` — kiến trúc, file map, nguyên tắc bất biến
   - `02-data-model.md` — bảng, trạng thái, vòng đời task/run/gate
   - `03-gates-and-autonomy.md` — gate flow, approve/reject, brakes, escalation
   - `04-tool-surface.md` — từng MCP tool + quirks + bẫy mapping
   - `05-agents-providers.md` — agent CLI/API, effort, quirks từng CLI, reasoning models
   - `06-context-rules.md` — project context & scoped rules (onboarding project mới)
   - `07-runtime-ops.md` — runbook vận hành, migrate, bảng sự cố quen
2. **`adr/`** — quyết định kiến trúc (ADR-001: unified tool registry).
3. **`plans/`** — plan đang thi hành. `GD4-CLEANUP-PLAN.md` = backlog sống
   (kèm task files ở `~/projects/control-tower/projects/agenticmatix/tasks/`).
4. **`reviews/`** — review + incident có giá trị lịch sử (vụ agy 2026-08-01).
5. **`testing/`** — kịch bản test hệ thống (B1.8) + sổ ghi nhận phát hiện.
6. **`coordinator-rules.md`** — nguồn sinh instruction files cho coordinator
   workspace (init-coordinator-workdir.sh nhúng nội dung này).

Quy ước: plan đã thi hành xong thì XÓA (git giữ lịch sử), phần kiến thức còn
giá trị phải được hấp thụ vào `spec/` trước khi xóa. Không giữ docs frontend —
FE đã khai tử.
