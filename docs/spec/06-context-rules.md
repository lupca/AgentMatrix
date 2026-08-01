# 06 — Project Context & Scoped Rules (onboarding project)

Mục tiêu: agent hiểu đúng dự án từ đầu → ít vòng lặp, ít token.
Hoàn thiện 2026-08-01 (CTV2-227, 4 vòng). Code: `services/context_generator.py`,
handler `_handle_save_project_context` (command_router), injection trong
`command_builder.py`.

## Flow thêm một project đang code dở

```
1. manage_project create {id, name, repo_root}  (+ approve admin gate)
2. create_task "Generate project context" --project <id>
   update_task: plan = hướng dẫn quét repo + gọi save_project_context
   (mẫu prompt: CONTEXT_GEN_PROMPT trong context_generator.py)
3. dispatch_task → executor CLI vào worktree, đọc repo
4. Executor gọi MCP tool save_project_context:
     {task_id: <task đang chạy — token scope>, project_id,
      context_md ≤150 dòng, rules: ≤5 {name, globs[], content}}
5. Từ đó mọi dispatch/review của project tự inject vào prompt:
     [Project Context] <context_md>
     [Project Rules]  ## <name> <content>   (chỉ rule có glob khớp task.files)
```

Lưu ý bước 3-4: task context-gen không commit code → run sẽ bị đánh failed ở
khâu RESULT_REF (gap CTV2-235) — context VẪN được lưu thành công; archive task
sau khi xong. Chờ dev làm loại task no-commit.

## context_md — khung chuẩn (≤150 dòng, handler từ chối nếu vượt)

```markdown
# Project: <name>
## Stack            (1 dòng)
## Hard Boundaries  (≤7 gạch — thứ vi phạm là vỡ)
## Key Patterns     (≤5 gạch — convention chính)
```
Đừng mô tả cây thư mục (stale nhanh). Boundaries > mô tả.

## Rules & glob matching

- Mỗi rule: `name` (unique per project, ≤100 ký tự), `globs` (list of strings,
  rỗng = áp mọi file), `content` (cắt 3000 ký tự). Save = THAY TRỌN BỘ rules cũ.
- Matching (`get_matching_rules`): task.files khớp glob nào thì inject rule đó;
  task không khai files → inject hết. Sắp theo `priority` giảm dần.
- `**/` khớp CẢ con trực tiếp (fnmatch thuần không làm được — đã vá
  `_glob_matches`, giữ hành vi này khi refactor).

## Bảo mật

- Tool executor-callable → schema PHẢI có `task_id`; `_task_scope_ok` đối chiếu
  token. Handler từ chối cross-project write (task.project phải == project_id).
- Coordinator token gọi trực tiếp cũng được (bỏ qua scope check) — dùng khi
  muốn đổ context tay.

## Trạng thái & kiểm tra

- `project.context_generated` + `ContextChecker.check_project_ready(project_id)`
  → `{has_context, has_rules, ready}`. Dispatch KHÔNG block khi thiếu context
  (inject là opportunistic — quyết định có chủ đích, đừng đổi thành block).
