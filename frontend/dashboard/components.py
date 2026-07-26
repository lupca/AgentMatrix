import os
import httpx
import streamlit as st
import pandas as pd
from datetime import datetime

API_URL = os.getenv("API_URL", "http://backend:8000").rstrip("/")

# Sample fallback tasks in case API backend is offline during standalone execution/preview
DEMO_TASKS = [
    {
        "id": "CTV2-001",
        "project": "control-tower-v2",
        "title": "Database Schema + Alembic Migrations",
        "status": "done",
        "priority": "high",
        "risk": "medium",
        "executor": "@antigravity-3.6-high",
        "reviewer": "@antigravity",
        "created_at": "2026-07-26",
        "updated_at": "2026-07-26",
        "deadline": "2026-08-05",
        "acceptance_criteria": [
            "PostgreSQL schema definitions created",
            "Alembic migration scripts included",
            "Rollback scripts verified"
        ],
        "plan": "1. Create models in app/db/models.py\n2. Configure Alembic env.py\n3. Generate initial migration",
        "result_ref": "PR-101",
        "verdict": "pass"
    },
    {
        "id": "CTV2-002",
        "project": "control-tower-v2",
        "title": "FastAPI CRUD + Pydantic Schemas",
        "status": "in-review",
        "priority": "high",
        "risk": "low",
        "executor": "@antigravity-3.6-high",
        "reviewer": "@antigravity",
        "created_at": "2026-07-26",
        "updated_at": "2026-07-26",
        "deadline": "2026-08-07",
        "acceptance_criteria": [
            "FastAPI app with /health endpoint",
            "Pydantic schemas for Task, Session, AuditLog",
            "CRUD endpoints for /api/tasks"
        ],
        "plan": "1. Implement schemas\n2. Build API routers\n3. Write pytest test cases",
        "result_ref": "PR-102",
        "verdict": "pending"
    },
    {
        "id": "CTV2-006",
        "project": "control-tower-v2",
        "title": "Chainlit Chat UI Integration",
        "status": "dispatched",
        "priority": "medium",
        "risk": "low",
        "executor": "@antigravity-3.6-high",
        "reviewer": "@antigravity",
        "created_at": "2026-07-26",
        "updated_at": "2026-07-26",
        "deadline": "2026-08-15",
        "acceptance_criteria": [
            "Chainlit chat interface connected to backend",
            "Streaming responses for agent conversation"
        ],
        "plan": "1. Setup chainlit config\n2. Connect backend websockets/http",
        "result_ref": None,
        "verdict": None
    },
    {
        "id": "CTV2-007",
        "project": "control-tower-v2",
        "title": "Streamlit Task Dashboard",
        "status": "dispatched",
        "priority": "medium",
        "risk": "low",
        "executor": "@antigravity-3.6-high",
        "reviewer": "@antigravity",
        "created_at": "2026-07-26",
        "updated_at": "2026-07-26",
        "deadline": "2026-08-18",
        "acceptance_criteria": [
            "Dashboard running on port 8501",
            "Display tasks from database",
            "Filters and sorting options",
            "Task detail expander",
            "Auto-refresh every 30s",
            "Docker container build success"
        ],
        "plan": "1. Create components.py\n2. Create app.py\n3. Create Dockerfile",
        "result_ref": None,
        "verdict": None
    },
    {
        "id": "PMI-023",
        "project": "topvnsport-pmi",
        "title": "Fix inventory sync bug",
        "status": "todo",
        "priority": "high",
        "risk": "high",
        "executor": None,
        "reviewer": "@antigravity",
        "created_at": "2026-07-25",
        "updated_at": "2026-07-25",
        "deadline": "2026-08-01",
        "acceptance_criteria": [
            "Prevent duplicate stock update calls",
            "Add idempotency key header"
        ],
        "plan": "1. Audit stock sync queue\n2. Implement lock mechanism",
        "result_ref": None,
        "verdict": None
    }
]

@st.cache_data(ttl=30)
def fetch_tasks_api(status=None, project=None, priority=None):
    """Fetch tasks from FastAPI backend with fallback to demo data if backend is unreachable."""
    params = {}
    if status and status != "all":
        params["status"] = status
    if project and project != "all":
        params["project"] = project
    if priority and priority != "all":
        params["priority"] = priority

    try:
        r = httpx.get(f"{API_URL}/api/tasks", params=params, timeout=3.0)
        if r.status_code == 200:
            return r.json(), False
    except Exception:
        pass
    
    # Fallback filtering logic for demo/offline mode
    filtered = DEMO_TASKS
    if status and status != "all":
        filtered = [t for t in filtered if t.get("status") == status]
    if project and project != "all":
        filtered = [t for t in filtered if t.get("project") == project]
    if priority and priority != "all":
        filtered = [t for t in filtered if t.get("priority") == priority]

    return filtered, True

def fetch_task_history_api(task_id):
    """Fetch history/audit log for a task."""
    try:
        r = httpx.get(f"{API_URL}/api/tasks/{task_id}/history", timeout=3.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return [
        {
            "timestamp": "2026-07-26T10:00:00",
            "action": "task_dispatched",
            "actor": "system",
            "details": f"Task {task_id} assigned to executor"
        }
    ]

def inject_custom_css():
    """Inject custom styling for modern glassmorphism aesthetic."""
    st.markdown("""
        <style>
        /* Main background & font styling */
        .stApp {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        
        /* Glassmorphism Header Card */
        .main-header {
            background: linear-gradient(135deg, rgba(30, 41, 59, 0.9), rgba(15, 23, 42, 0.95));
            padding: 1.5rem 2rem;
            border-radius: 12px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
            border: 1px solid rgba(255, 255, 255, 0.1);
            margin-bottom: 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .main-header h1 {
            color: #F8FAFC;
            margin: 0;
            font-size: 1.8rem;
            font-weight: 700;
            letter-spacing: -0.5px;
        }
        
        .main-header p {
            color: #94A3B8;
            margin: 0.2rem 0 0 0;
            font-size: 0.9rem;
        }

        /* Metric Pill Cards */
        .stat-container {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
            gap: 0.75rem;
            margin-bottom: 1.5rem;
        }

        .stat-card {
            background: #1E293B;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 0.85rem 1rem;
            text-align: center;
            transition: transform 0.2s ease, border-color 0.2s ease;
        }

        .stat-card:hover {
            transform: translateY(-2px);
            border-color: #38BDF8;
        }

        .stat-card .number {
            font-size: 1.6rem;
            font-weight: 700;
            margin-top: 0.2rem;
        }

        .stat-card .label {
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #94A3B8;
            font-weight: 600;
        }

        /* Status Colors */
        .status-todo { color: #FBBF24; }
        .status-dispatched { color: #38BDF8; }
        .status-in-review { color: #C084FC; }
        .status-done { color: #34D399; }
        .status-total { color: #F8FAFC; }

        /* Badge formatting */
        .badge {
            display: inline-block;
            padding: 0.25rem 0.6rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
            text-align: center;
        }
        .badge-todo { background-color: rgba(251, 191, 36, 0.15); color: #FBBF24; border: 1px solid rgba(251, 191, 36, 0.3); }
        .badge-dispatched { background-color: rgba(56, 189, 248, 0.15); color: #38BDF8; border: 1px solid rgba(56, 189, 248, 0.3); }
        .badge-in-review { background-color: rgba(192, 132, 252, 0.15); color: #C084FC; border: 1px solid rgba(192, 132, 252, 0.3); }
        .badge-done { background-color: rgba(52, 211, 153, 0.15); color: #34D399; border: 1px solid rgba(52, 211, 153, 0.3); }
        
        .badge-high { background-color: rgba(248, 113, 113, 0.15); color: #F87171; }
        .badge-medium { background-color: rgba(251, 146, 60, 0.15); color: #FB923C; }
        .badge-low { background-color: rgba(148, 163, 184, 0.15); color: #94A3B8; }

        /* Task detail card */
        .detail-box {
            background: #0F172A;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 1.25rem;
            margin-top: 1rem;
        }
        </style>
    """, unsafe_allow_html_script=True)

def render_header(is_offline=False):
    """Render top header with title and status badge."""
    col_title, col_btn = st.columns([4, 1])
    with col_title:
        status_indicator = "🟢 API Connected" if not is_offline else "🟠 Offline Mode (Demo)"
        st.markdown(
            f"""
            <div style="display: flex; align-items: center; gap: 1rem;">
                <h1 style="margin: 0; padding: 0;">🛰️ Control Tower Dashboard</h1>
                <span style="font-size: 0.85rem; padding: 0.2rem 0.6rem; border-radius: 12px; background: #1E293B; border: 1px solid #334155; color: #94A3B8;">
                    {status_indicator}
                </span>
            </div>
            <p style="color: #94A3B8; margin-top: 0.3rem;">Real-time task tracking, orchestration metrics, & verification gates</p>
            """,
            unsafe_allow_html=True
        )
    with col_btn:
        st.write("")
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

def render_stats_bar(tasks):
    """Render summary statistics cards."""
    todo_count = len([t for t in tasks if t.get("status") == "todo"])
    dispatched_count = len([t for t in tasks if t.get("status") == "dispatched"])
    in_review_count = len([t for t in tasks if t.get("status") == "in-review"])
    done_count = len([t for t in tasks if t.get("status") == "done"])
    total_count = len(tasks)

    st.markdown(
        f"""
        <div class="stat-container">
            <div class="stat-card">
                <div class="label">Total Tasks</div>
                <div class="number status-total">{total_count}</div>
            </div>
            <div class="stat-card">
                <div class="label">🟡 Todo</div>
                <div class="number status-todo">{todo_count}</div>
            </div>
            <div class="stat-card">
                <div class="label">🔵 Dispatched</div>
                <div class="number status-dispatched">{dispatched_count}</div>
            </div>
            <div class="stat-card">
                <div class="label">🟣 In-Review</div>
                <div class="number status-in-review">{in_review_count}</div>
            </div>
            <div class="stat-card">
                <div class="label">🟢 Done</div>
                <div class="number status-done">{done_count}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

def get_status_icon(status):
    mapping = {
        "todo": "🟡 todo",
        "dispatched": "🔵 dispatched",
        "in-review": "🟣 in-review",
        "done": "🟢 done",
        "cancelled": "🔴 cancelled"
    }
    return mapping.get(status, status)

def render_task_detail(task):
    """Render full information detail view for a selected task."""
    st.subheader(f"📋 Task Detail: {task['id']} — {task['title']}")
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(f"**Project:** `{task.get('project', '-')}`")
        st.markdown(f"**Status:** {get_status_icon(task.get('status', '-'))}")
    with col2:
        st.markdown(f"**Priority:** `{task.get('priority', '-')}`")
        st.markdown(f"**Risk:** `{task.get('risk', '-')}`")
    with col3:
        st.markdown(f"**Executor:** `{task.get('executor') or '-'}`")
        st.markdown(f"**Reviewer:** `{task.get('reviewer') or '-'}`")
    with col4:
        st.markdown(f"**Deadline:** `{task.get('deadline') or '-'}`")
        st.markdown(f"**Updated:** `{task.get('updated_at') or '-'}`")
    
    st.divider()
    
    tab_ac, tab_plan, tab_result, tab_history = st.tabs(["Acceptance Criteria", "Implementation Plan", "Verdict & Output", "Audit History"])
    
    with tab_ac:
        ac_list = task.get("acceptance_criteria") or []
        if isinstance(ac_list, list) and ac_list:
            for idx, item in enumerate(ac_list, 1):
                st.checkbox(item, value=True if task.get("status") == "done" else False, key=f"ac_{task['id']}_{idx}", disabled=True)
        else:
            st.info("No acceptance criteria defined for this task.")

    with tab_plan:
        plan_text = task.get("plan")
        if plan_text:
            st.markdown(plan_text)
        else:
            st.info("No implementation plan recorded.")

    with tab_result:
        res_col1, res_col2 = st.columns(2)
        with res_col1:
            st.markdown(f"**Result Reference:** `{task.get('result_ref') or 'N/A'}`")
        with res_col2:
            st.markdown(f"**Verdict:** `{task.get('verdict') or 'Pending'}`")

    with tab_history:
        history = fetch_task_history_api(task["id"])
        if history:
            st.dataframe(pd.DataFrame(history), use_container_width=True)
        else:
            st.caption("No audit history found.")
