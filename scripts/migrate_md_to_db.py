#!/usr/bin/env python3
"""
CTV2-014: Migration script - Markdown to PostgreSQL
Imports projects, tasks, and agents from control-tower markdown files.
"""
import os
import re
import sys
import yaml
import argparse
from pathlib import Path
from datetime import datetime
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

CT_ROOT = Path(os.getenv("CT_ROOT", "/home/lupca/projects/control-tower"))
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ct:secret@localhost:5433/control_tower")


def parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from markdown content."""
    match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


def parse_project_registry(index_path: Path) -> list[dict]:
    """Parse PROJECT REGISTRY table from index.md."""
    content = index_path.read_text()
    projects = []

    in_table = False
    for line in content.split('\n'):
        if '| Project' in line and 'repo_root' in line:
            in_table = True
            continue
        if in_table and line.startswith('|'):
            if ':---' in line:
                continue
            cols = [c.strip() for c in line.split('|')[1:-1]]
            if len(cols) >= 2:
                slug = cols[0].strip('`').strip()
                repo_root = cols[1].strip('`').strip()
                if slug and not slug.startswith('Project'):
                    projects.append({
                        'id': slug,
                        'name': slug.replace('-', ' ').title(),
                        'repo_root': repo_root,
                    })
        elif in_table and not line.strip().startswith('|'):
            break

    return projects


def parse_task_files(projects_dir: Path) -> list[dict]:
    """Parse all task markdown files."""
    tasks = []
    for task_file in projects_dir.glob('*/tasks/*.md'):
        content = task_file.read_text()
        fm = parse_frontmatter(content)
        if not fm.get('id'):
            continue

        project = task_file.parent.parent.name
        tasks.append({
            'id': fm.get('id'),
            'project': project,
            'title': fm.get('title', ''),
            'status': fm.get('status', 'todo'),
            'priority': fm.get('priority'),
            'risk': fm.get('risk'),
            'executor': fm.get('executor'),
            'reviewer': fm.get('reviewer'),
            'acceptance_criteria': fm.get('acceptance_criteria', []),
            'files': fm.get('files', []),
            'tests': fm.get('tests', []),
            'plan': fm.get('plan'),
            'deadline': fm.get('deadline'),
        })

    return tasks


def parse_agent_files(agents_dir: Path) -> list[dict]:
    """Parse agent profile markdown files."""
    agents = []
    for agent_file in agents_dir.glob('@*.md'):
        content = agent_file.read_text()
        fm = parse_frontmatter(content)

        agent_id = fm.get('agent_id', agent_file.stem)
        agents.append({
            'id': agent_id,
            'type': fm.get('type', 'ai'),
            'model': fm.get('model'),
            'effort': fm.get('effort'),
            'total_tasks_executed': fm.get('total_tasks_executed', 0),
            'total_tasks_reviewed': fm.get('total_tasks_reviewed', 0),
            'success_rate': fm.get('success_rate', 1.0),
            'strengths': fm.get('strengths', []),
            'weaknesses': fm.get('weaknesses', []),
            'status': 'active' if fm.get('status') != 'deprecated' else 'deprecated',
        })

    return agents


def migrate(dry_run: bool = False):
    """Run the migration."""
    print(f"Source: {CT_ROOT}")
    print(f"Database: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")
    print(f"Dry run: {dry_run}")
    print("-" * 50)

    # Parse data
    projects = parse_project_registry(CT_ROOT / 'index.md')
    tasks = parse_task_files(CT_ROOT / 'projects')
    agents = parse_agent_files(CT_ROOT / 'knowledge' / 'agents')

    print(f"Parsed: {len(projects)} projects, {len(tasks)} tasks, {len(agents)} agents")

    if dry_run:
        print("\n[DRY RUN] Would import:")
        for p in projects[:5]:
            print(f"  Project: {p['id']}")
        if len(projects) > 5:
            print(f"  ... and {len(projects) - 5} more")
        for t in tasks[:5]:
            print(f"  Task: {t['id']} ({t['status']})")
        if len(tasks) > 5:
            print(f"  ... and {len(tasks) - 5} more")
        for a in agents[:5]:
            print(f"  Agent: {a['id']} ({a['type']})")
        if len(agents) > 5:
            print(f"  ... and {len(agents) - 5} more")
        return

    # Connect to database
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Upsert projects
        for p in projects:
            session.execute(text("""
                INSERT INTO projects (id, name, repo_root)
                VALUES (:id, :name, :repo_root)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    repo_root = EXCLUDED.repo_root,
                    updated_at = NOW()
            """), p)

        # Upsert tasks
        for t in tasks:
            session.execute(text("""
                INSERT INTO tasks (id, project, title, status, priority, risk, executor, reviewer, acceptance_criteria, files, tests, plan, deadline)
                VALUES (:id, :project, :title, :status, :priority, :risk, :executor, :reviewer, :acceptance_criteria, :files, :tests, :plan, :deadline)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    priority = EXCLUDED.priority,
                    risk = EXCLUDED.risk,
                    executor = EXCLUDED.executor,
                    reviewer = EXCLUDED.reviewer,
                    acceptance_criteria = EXCLUDED.acceptance_criteria,
                    files = EXCLUDED.files,
                    tests = EXCLUDED.tests,
                    plan = EXCLUDED.plan,
                    deadline = EXCLUDED.deadline,
                    updated_at = NOW()
            """), {
                **t,
                'acceptance_criteria': str(t['acceptance_criteria']),
                'files': str(t['files']),
                'tests': str(t['tests']),
            })

        # Upsert agents
        for a in agents:
            session.execute(text("""
                INSERT INTO agents (id, type, model, effort, total_tasks_executed, total_tasks_reviewed, success_rate, strengths, weaknesses, status)
                VALUES (:id, :type, :model, :effort, :total_tasks_executed, :total_tasks_reviewed, :success_rate, :strengths, :weaknesses, :status)
                ON CONFLICT (id) DO UPDATE SET
                    type = EXCLUDED.type,
                    model = EXCLUDED.model,
                    effort = EXCLUDED.effort,
                    total_tasks_executed = EXCLUDED.total_tasks_executed,
                    total_tasks_reviewed = EXCLUDED.total_tasks_reviewed,
                    success_rate = EXCLUDED.success_rate,
                    strengths = EXCLUDED.strengths,
                    weaknesses = EXCLUDED.weaknesses,
                    status = EXCLUDED.status,
                    updated_at = NOW()
            """), {
                **a,
                'strengths': str(a['strengths']),
                'weaknesses': str(a['weaknesses']),
            })

        session.commit()
        print(f"\nImported: {len(projects)} projects, {len(tasks)} tasks, {len(agents)} agents")

    except Exception as e:
        session.rollback()
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        session.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Migrate markdown to PostgreSQL')
    parser.add_argument('--dry-run', action='store_true', help='Preview without committing')
    args = parser.parse_args()

    migrate(dry_run=args.dry_run)
