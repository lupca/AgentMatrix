from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import Task as TaskModel, Project as ProjectModel, Agent as AgentModel
from app.schemas.stats import ProjectStats, AgentStats

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/overview")
def get_stats_overview(db: Session = Depends(get_db)):
    tasks = db.query(TaskModel).all()
    total_tasks = len(tasks)

    by_status: dict[str, int] = {}
    for task in tasks:
        st = task.status or "unknown"
        by_status[st] = by_status.get(st, 0) + 1

    done_tasks = by_status.get("done", 0) + by_status.get("completed", 0) + by_status.get("passed", 0)
    inactive_count = done_tasks + by_status.get("cancelled", 0) + by_status.get("failed", 0)
    active_tasks = max(0, total_tasks - inactive_count)

    return {
        "totalTasks": total_tasks,
        "completedTasks": done_tasks,
        "activeGates": active_tasks,
        "tasksByStatus": by_status,
    }


@router.get("/projects", response_model=list[ProjectStats])
def get_stats_projects(db: Session = Depends(get_db)):
    projects = db.query(ProjectModel).all()
    project_map = {p.id: p.name for p in projects}

    tasks = db.query(TaskModel).all()

    stats_by_project: dict[str, dict] = {}

    for proj in projects:
        stats_by_project[proj.id] = {
            "project_id": proj.id,
            "project_name": proj.name,
            "total_tasks": 0,
            "done_tasks": 0,
            "active_tasks": 0,
            "by_status": {}
        }

    for task in tasks:
        pid = task.project or "unassigned"
        if pid not in stats_by_project:
            stats_by_project[pid] = {
                "project_id": pid,
                "project_name": project_map.get(pid, pid),
                "total_tasks": 0,
                "done_tasks": 0,
                "active_tasks": 0,
                "by_status": {}
            }

        entry = stats_by_project[pid]
        entry["total_tasks"] += 1
        st = task.status or "unknown"
        entry["by_status"][st] = entry["by_status"].get(st, 0) + 1

    result = []
    for pid, data in stats_by_project.items():
        by_st = data["by_status"]
        done_cnt = by_st.get("done", 0) + by_st.get("completed", 0) + by_st.get("passed", 0)
        inactive_cnt = done_cnt + by_st.get("cancelled", 0) + by_st.get("failed", 0)
        act_cnt = max(0, data["total_tasks"] - inactive_cnt)

        result.append(
            ProjectStats(
                project_id=data["project_id"],
                project_name=data["project_name"],
                total_tasks=data["total_tasks"],
                done_tasks=done_cnt,
                active_tasks=act_cnt,
                by_status=by_st
            )
        )

    return result


@router.get("/agents", response_model=list[AgentStats])
def get_stats_agents(db: Session = Depends(get_db)):
    agents = db.query(AgentModel).all()
    tasks = db.query(TaskModel).all()

    agent_stats: dict[str, dict] = {}

    for agent in agents:
        agent_stats[agent.id] = {
            "agent_id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "tasks_executed": 0,
            "tasks_reviewed": 0,
            "tasks_completed": 0,
            "active_tasks": 0,
        }

    for task in tasks:
        st = task.status or "unknown"
        is_done = st in ("done", "completed", "passed")
        is_active = st not in ("done", "completed", "passed", "cancelled", "failed")

        if task.executor:
            if task.executor not in agent_stats:
                agent_stats[task.executor] = {
                    "agent_id": task.executor,
                    "name": task.executor,
                    "role": "executor",
                    "tasks_executed": 0,
                    "tasks_reviewed": 0,
                    "tasks_completed": 0,
                    "active_tasks": 0,
                }
            agent_stats[task.executor]["tasks_executed"] += 1
            if is_done:
                agent_stats[task.executor]["tasks_completed"] += 1
            if is_active:
                agent_stats[task.executor]["active_tasks"] += 1

        if task.reviewer:
            if task.reviewer not in agent_stats:
                agent_stats[task.reviewer] = {
                    "agent_id": task.reviewer,
                    "name": task.reviewer,
                    "role": "reviewer",
                    "tasks_executed": 0,
                    "tasks_reviewed": 0,
                    "tasks_completed": 0,
                    "active_tasks": 0,
                }
            agent_stats[task.reviewer]["tasks_reviewed"] += 1

    result = []
    for aid, data in agent_stats.items():
        executed = data["tasks_executed"]
        completed = data["tasks_completed"]
        success_rate = (completed / executed) if executed > 0 else 1.0

        result.append(
            AgentStats(
                agent_id=data["agent_id"],
                name=data["name"],
                role=data["role"],
                tasks_executed=executed,
                tasks_reviewed=data["tasks_reviewed"],
                tasks_completed=completed,
                success_rate=round(success_rate, 4),
                active_tasks=data["active_tasks"],
            )
        )

    return result
