#!/usr/bin/env python3
"""Fix 5 drifted spec anchors caught by the staleness detector (CTV2-1386).

Commits 0b6d082 and c446453 changed tool_registry.py, command_router.py,
task_orchestration.py, and test_tool_registry.py without updating the
corresponding spec items.  The staleness detector (fixed in 3bca751) caught
all 5 drifts on its first live run.

Three SpecItems affected:

  7749e3f2  MCP native server port 8100 — spec still accurate, anchor
            recomputed (TOOL_REGISTRY gained critique_spec_plan + description).
  19684fa5  Tool registry — body updated to document tool_argument_validator,
            JSON create_task parameters, and critique_spec_plan tool.
  ac1cf7fb  Task lifecycle — body updated to document optional critic fields
            in write_spec_plan and record_plan_critic_verdict entry point.

All three promoted from draft to active.  Anchor SHAs recomputed via
compute_anchor_sha — never hardcoded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.db.base import SessionLocal
from app.db.models import SpecAnchor, SpecItem
from app.services.spec_anchor import compute_anchor_sha, resolve_repo_root

BODIES = {
    "19684fa5-62f0-4955-8eaa-7803423645fb": (
        "TOOL_REGISTRY dict chứa ToolSpec cho mỗi tool. ToolSpec gồm: name, "
        "description, parameters (JSON Schema OpenAI format), handler (dispatch "
        "to CommandRouter._handle_<handler>), tier (eager/deferred), permission "
        "(read/write/admin), entity, group, required_role. Deferred groups: "
        "task_lifecycle, admin, session, research, query, spec. "
        "get_tool_definitions() và CommandRouter đều đọc từ TOOL_REGISTRY — "
        "không có khai báo trùng.\n\n"
        "Kiểm tham số nghiêm ngặt (tool_argument_validator.py): trước khi vào "
        "handler, mọi tool call được validate — tham số lạ bị từ chối (không "
        "nuốt lặng lẽ), tham số required thiếu bị báo lỗi, sai kiểu bị chặn. "
        "Thứ tự: xác thực → phân quyền → kiểm tham số → mở DB.\n\n"
        "create_task nhận tham số dạng JSON (theo khuôn mẫu manage_inbox), bao "
        "gồm trường description lưu vào raw_input. Handler vẫn nhận chuỗi cờ "
        "để /pm gõ tay không gãy.\n\n"
        "Tool critique_spec_plan: chạy riêng plan critic trên plan đã lưu, "
        "không gọi lại planner. Dùng để retry critic failed mà không đốt thêm "
        "planner call.\n\n"
        "Chứng cứ: backend/app/services/tool_registry.py (ToolSpec, "
        "TOOL_REGISTRY, DEFERRED_GROUPS), backend/app/services/command_router.py "
        "(execute_tool), backend/app/services/tool_argument_validator.py "
        "(validate_tool_arguments), backend/tests/test_tool_registry.py."
    ),
    "ac1cf7fb-86dd-4304-8dd9-00ced8721831": (
        "Task trải qua các gate: spec → dispatch → review → verdict. Status "
        "flow: todo → dispatched (khi request_dispatch) → awaiting-review "
        "(executor xong) → in-review (reviewer bắt đầu) → done (verdict pass) "
        "hoặc changes-requested (verdict fail, re-dispatch). Cas_status() dùng "
        "compare-and-set với version column để chống concurrent transition. "
        "TaskOrchestrationService là application service DUY NHẤT được phép "
        "mutate lifecycle fields.\n\n"
        "Spec plan gate: write_spec_plan nhận plan từ planner và có thể nhận "
        "luôn critic fields (critic, critic_verdict, critic_findings, "
        "critic_summary, critic_tokens) — tất cả đều optional. Nếu critic step "
        "fail sau khi plan đã ghi, plan vẫn nằm trên task. "
        "record_plan_critic_verdict là entry point độc lập để ghi critic "
        "verdict sau đó, mỗi lần gọi append một GateRecord mới vào plan_critic.\n\n"
        "Chứng cứ: backend/app/services/task_orchestration.py "
        "(TaskOrchestrationService, write_spec_plan, record_plan_critic_verdict), "
        "backend/app/services/task_state_machine.py (cas_status, apply_gate, "
        "write_spec_plan, record_plan_critic_verdict), backend/app/db/models.py "
        "(Task.workflow_state property)."
    ),
}

DRIFTED_ANCHORS = {
    "7749e3f2-2326-4ed0-910a-f0fb31633aac": [
        ("backend/app/services/tool_registry.py", "TOOL_REGISTRY"),
    ],
    "19684fa5-62f0-4955-8eaa-7803423645fb": [
        ("backend/app/services/tool_registry.py", "TOOL_REGISTRY"),
        ("backend/app/services/command_router.py", "execute_tool"),
        ("backend/tests/test_tool_registry.py", "test_registry_has_tools_with_unique_names"),
    ],
    "ac1cf7fb-86dd-4304-8dd9-00ced8721831": [
        ("backend/app/services/task_orchestration.py", "TaskOrchestrationService"),
    ],
}


def fix(*, dry_run: bool = False) -> dict:
    db = SessionLocal()
    try:
        root = resolve_repo_root(db, "agenticmatix")
        updated_bodies = 0
        updated_anchors = 0

        for item_id, body in BODIES.items():
            item = db.get(SpecItem, item_id)
            if item is None:
                continue
            if item.body != body:
                updated_bodies += 1
                if not dry_run:
                    item.body = body

        for item_id, anchor_keys in DRIFTED_ANCHORS.items():
            item = db.get(SpecItem, item_id)
            if item is None:
                continue
            for a in item.anchors:
                if (a.path, a.symbol) in anchor_keys:
                    new_sha = compute_anchor_sha(root, a.path, a.symbol)
                    if new_sha and new_sha != a.anchor_sha:
                        updated_anchors += 1
                        if not dry_run:
                            a.anchor_sha = new_sha

            if not dry_run:
                item.status = "active"
                item.stale_reason = None

        if not dry_run:
            db.flush()
            db.commit()

        total = db.query(SpecAnchor).join(SpecItem).filter(
            SpecItem.project_id == "agenticmatix",
            SpecItem.archived_at.is_(None),
        ).count()
        matched = 0
        drifted = 0
        for a in db.query(SpecAnchor).join(SpecItem).filter(
            SpecItem.project_id == "agenticmatix",
            SpecItem.archived_at.is_(None),
        ).all():
            current = compute_anchor_sha(root, a.path, a.symbol)
            if current == a.anchor_sha:
                matched += 1
            else:
                drifted += 1

        return {
            "updated_bodies": updated_bodies,
            "updated_anchors": updated_anchors,
            "total_anchors": total,
            "matched": matched,
            "drifted": drifted,
            "dry_run": dry_run,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(fix(dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
