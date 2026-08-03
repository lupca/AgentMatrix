# 05 — Agents & providers (CLI / API)

## Hai loại agent

**CLI agent** (`agent_type="cli"`): chạy bằng subscription CLI trên máy —
`claude`, `agy` (Antigravity), `codex`. Không cần API key.
**API agent** (`agent_type="api"`): gọi thẳng endpoint OpenAI-compatible —
`provider` (openai/anthropic/google), `model`, `api_key` (mã hóa 1 chiều,
không đọc lại được), `base_url` tùy chọn (vd `https://api.siliconflow.com/v1`
cho DeepSeek/GLM/Kimi/LongCat).

## Hai đường build lệnh CLI (dễ lẫn!)

1. **Executor/Reviewer dispatch** — `command_builder.py`
   (`build_dispatch_command` / `build_review_command`): prompt đầy đủ task +
   context/rules injection + RESULT_REF contract; chạy trong worktree.
2. **Coordinator / spec-plan / LLMService** — `cli_dispatcher.build_cli_command`:
   dùng bởi `CLIProvider` khi một agent CLI được gọi như một "LLM"
   (generate_spec_plan, coordinator turns). Chạy trong cwd của server.

Sửa hành vi CLI phải sửa CẢ HAI (bài học CTV2-236: effort chỉ có ở đường 1).

## Effort — quy tắc thống nhất

- Model name mang suffix (`-low/-medium/-high/-extra-high/-max/-ultra`,
  vd `gemini-3.6-flash-high`, `claude-sonnet-high` KHÔNG tính — đó là agent id):
  không truyền flag effort.
- Ngược lại truyền theo CLI: claude `--effort X`; agy `--effort X`;
  codex `-c model_reasoning_effort=X`.
- agy: một số model BẮT BUỘC effort (`gemini-3.6-flash` — thiếu là exit 1
  "invalid model selection"), một số model TỪ CHỐI flag (`gemini-2.5-pro`) —
  vì vậy chỉ truyền khi agent có cấu hình `effort`.

## Quirks từng CLI

- **agy 1.1.9**: prompt PHẢI đứng ngay sau `--print` (flag chen giữa làm agy
  nuốt prompt — CTV2-211). Headless hay làm việc trong scratch dir
  (`~/.gemini/antigravity-cli/scratch/`) thay vì cwd được spawn (CTV2-220) →
  KHÔNG tin cậy làm executor; đã bắt quả tang rubber-stamp khi làm reviewer
  (pass 4/4 trong khi bug HIGH còn nguyên) → hạn chế cả reviewer.
  Không hỗ trợ `--mcp-config`.
- **claude**: `-p` im lặng đến khi xong → dính watchdog no-progress (xem 03).
  `--dangerously-skip-permissions` cho executor; reviewer read-only.
  Hai review Opus đo ngày 2026-08-04 thêm metadata top-level ngoài template:
  CTV2-1345 dùng `toolchain_notes`; CTV2-1342 dùng `toolchain_output` + `notes`.
  Parser chỉ map đúng các alias này vào `toolchain_results`; validation strict
  với field khác vẫn giữ nguyên.
- **codex**: `codex exec -m <model>`; bypass sandbox bằng
  `--dangerously-bypass-approvals-and-sandbox` (chỉ đường dispatch).

## API path — reasoning models

`OpenAIAdapter._combine_text` trả `<think>{reasoning}</think>{content}` cho
model có `reasoning_content` (DeepSeek-V4, GLM-5.2...). Consumer nào parse
JSON từ text PHẢI bóc think-block trước (spec_plan_generator đã làm; consumer
mới thì nhớ). Reasoning ngốn token: budget cho call sinh JSON nên ≥4096.

## Chọn agent

- `AgentSuggester`/matcher xếp theo capabilities + success_rate (số đo thật).
- CTV2-228 (mở): agent_id chỉ định lúc dispatch bị matcher ghi đè lúc approve.
- CTV2-223 (mở): bảng agents có id người (@user, @lupca, @dev-tung) — matcher
  có thể gán nhầm vai máy.
- MCP attach lúc spawn (`mcp_attach.attach_mcp`): claude nhận `--mcp-config`
  + token executor scope theo task; agy bị bỏ qua; token TTL = run timeout + grace.
