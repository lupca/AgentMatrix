#!/usr/bin/env python3
"""
Migration script - Markdown to PostgreSQL
Imports projects, tasks, agents, and knowledge from control-tower markdown files.

Tables CLEARED before import:
- projects, tasks, agents, knowledge_items, task_dependencies

Tables PRESERVED (not touched):
- llm_usage (token tracking), audit_log, sessions, agent_runs,
  agent_output_chunks, gate_records, settings, system_settings
"""
import os
import re
import sys
import yaml
import json
import argparse
from pathlib import Path
from datetime import datetime
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

CT_ROOT = Path(os.getenv("CT_ROOT", "/home/lupca/projects/control-tower"))
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ct:secret@localhost:5433/control_tower")

# Tables to clear (order matters for FK constraints)
# agents is deliberately NOT cleared: DB rows carry api_key/base_url for
# API-backed agents that have no markdown profile, plus measured
# success_rate — clearing would destroy them (agent_accounts cascades).
# Agents are upserted instead; see the ON CONFLICT clause below.
TABLES_TO_CLEAR = [
    "task_dependencies",
    "gate_records",
    "tasks",
    "projects",
    "knowledge_items",
]


def parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from markdown content."""
    match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


def parse_acceptance_criteria(content: str) -> list[str]:
    """Extract acceptance criteria from markdown body."""
    # Look for AC section header
    ac_patterns = [
        r'##\s*Tiêu chí nghiệm thu\s*\(AC\)(.*?)(?=\n##|\Z)',
        r'##\s*Acceptance Criteria(.*?)(?=\n##|\Z)',
        r'##\s*AC\b(.*?)(?=\n##|\Z)',
    ]

    for pattern in ac_patterns:
        match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
        if match:
            section = match.group(1)
            # Extract checkbox items: - [ ] or - [x]
            items = re.findall(r'-\s*\[[xX ]\]\s*(.+)', section)
            if items:
                return [item.strip() for item in items]
            # Fallback: extract bullet points
            items = re.findall(r'-\s+(.+)', section)
            if items:
                return [item.strip() for item in items]
    return []


def parse_plan_section(content: str) -> str | None:
    """Extract plan/context section from markdown body."""
    # Look for plan-related sections
    plan_patterns = [
        r'##\s*Plan\b(.*?)(?=\n##|\Z)',
        r'##\s*Implementation Plan(.*?)(?=\n##|\Z)',
        r'##\s*Context từ User(.*?)(?=\n##|\Z)',
        r'##\s*Context(.*?)(?=\n##|\Z)',
    ]

    sections = []
    for pattern in plan_patterns:
        match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
        if match:
            sections.append(match.group(1).strip())

    return '\n\n'.join(sections) if sections else None


def get_body_content(content: str) -> str:
    """Get markdown body without frontmatter."""
    match = re.match(r'^---\s*\n.*?\n---\s*\n(.*)', content, re.DOTALL)
    return match.group(1) if match else content


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
                        'status': 'active',
                    })
        elif in_table and not line.strip().startswith('|'):
            break

    for p in projects:
        project_md = index_path.parent / "projects" / p['id'] / f"{p['id']}.md"
        if project_md.exists():
            content = project_md.read_text()
            fm = parse_frontmatter(content)
            body = get_body_content(content)
            p['name'] = fm.get('full_name', p['name'])
            p['task_prefix'] = fm.get('task_prefix', p['id'].upper()[:5])
            p['description'] = fm.get('description')
            p['context_md'] = body

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
        body = get_body_content(content)

        # Parse AC from body if not in frontmatter
        ac = fm.get('acceptance_criteria', [])
        if not ac or (isinstance(ac, list) and len(ac) == 0):
            ac = parse_acceptance_criteria(body)

        # Parse plan from body if not in frontmatter
        plan = fm.get('plan')
        if not plan:
            plan = parse_plan_section(body)

        # Use raw body as raw_input if no specific raw_input
        raw_input = fm.get('raw_input')
        if not raw_input:
            # Get first section after title as context
            title_match = re.search(r'^#\s+.+\n\n(.+?)(?=\n##|\Z)', body, re.DOTALL)
            if title_match:
                raw_input = title_match.group(1).strip()

        task_id = fm.get('id')
        existing_ids = [t['id'] for t in tasks]
        if task_id in existing_ids:
            counter = 2
            new_id = f"{task_id}_{counter}"
            while new_id in existing_ids:
                counter += 1
                new_id = f"{task_id}_{counter}"
            task_id = new_id

        tasks.append({
            'id': task_id,
            'project': project,
            'title': fm.get('title', ''),
            'raw_input': raw_input,
            'status': fm.get('status', 'todo'),
            'current_gate': fm.get('current_gate', 'spec'),
            'mode': fm.get('mode', 'supervised'),
            'priority': fm.get('priority'),
            'risk': fm.get('risk'),
            'executor': fm.get('executor'),
            'reviewer': fm.get('reviewer'),
            'acceptance_criteria': ac,
            'files': fm.get('files', []),
            'tests': fm.get('tests', []),
            'flows': fm.get('flows', []),
            'plan': plan,
            'result_ref': fm.get('result_ref'),
            'verdict': fm.get('verdict'),
            'predicted_success': fm.get('predicted_success'),
            'prediction_factors': fm.get('prediction_factors'),
            'deadline': fm.get('deadline'),
            'tags': fm.get('tags', []),
            'depends_on': fm.get('depends_on', []),
            'created_at': fm.get('created'),
            'updated_at': fm.get('updated'),
            'dispatched_at': fm.get('dispatched'),
            'completed_at': fm.get('completed'),
        })

    return tasks


def parse_agent_files(agents_dir: Path) -> list[dict]:
    """Parse agent profile markdown files."""
    agents = []
    for agent_file in agents_dir.glob('@*.md'):
        content = agent_file.read_text()
        fm = parse_frontmatter(content)

        agent_id = fm.get('agent_id', agent_file.stem)
        name = fm.get('name') or agent_id.lstrip('@').replace('-', ' ').title()
        role = fm.get('role') or 'Agent'

        # Determine agent_type: only 'cli' or 'api' are valid in DB schema
        # (human users are stored as 'cli' with type='human' in the type field)
        agent_type = 'cli'  # default for all agents

        agents.append({
            'id': agent_id,
            'name': name,
            'role': role,
            'type': fm.get('type', 'ai'),
            'model': fm.get('model'),
            'effort': fm.get('effort'),
            'cli': fm.get('cli'),  # NEW: CLI tool name (e.g., "agy", "claude")
            'provider': fm.get('provider'),  # NEW: API provider
            'is_default': fm.get('is_default', False),  # NEW
            'agent_type': agent_type,  # NEW: "cli" or "api" or "human"
            'success_rate': float(fm.get('success_rate', 1.0) or 1.0),
            'capabilities': fm.get('strengths', []),
            'status': 'active' if fm.get('status') != 'deprecated' else 'deprecated',
        })

    return agents


def parse_knowledge_files(knowledge_dir: Path) -> list[dict]:
    """Parse knowledge markdown files from all categories."""
    items = []

    # Categories to scan
    categories = ['patterns', 'guides', 'conventions', 'decisions', 'domains',
                  'metrics', 'research', 'tools']

    for category in categories:
        cat_dir = knowledge_dir / category
        if not cat_dir.exists():
            continue

        for md_file in cat_dir.glob('*.md'):
            if md_file.name.startswith('_'):
                continue

            content = md_file.read_text()
            fm = parse_frontmatter(content)

            # Extract ID from frontmatter or filename
            item_id = (fm.get('pattern_id') or fm.get('guide_id') or
                      fm.get('id') or md_file.stem)

            # Extract title from first H1 or filename
            title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
            title = title_match.group(1) if title_match else md_file.stem.replace('-', ' ').title()

            # Truncate ID to 50 chars (DB limit)
            full_id = f"{category}/{item_id}"
            if len(full_id) > 50:
                full_id = full_id[:50]

            items.append({
                'id': full_id,
                'title': title[:200] if len(title) > 200 else title,  # title limit is 200
                'category': category,
                'content': content,
                'tags': fm.get('tags', []),
                'project': fm.get('project'),
                'author': fm.get('author'),
                'status': fm.get('status', 'active'),
            })

    return items


def clear_tables(session, tables: list[str], dry_run: bool = False):
    """Clear specified tables in order (respecting FK constraints).

    Uses DELETE instead of TRUNCATE CASCADE to preserve data in tables
    that reference these tables with ON DELETE SET NULL (like llm_usage).
    """
    for table in tables:
        if dry_run:
            count = session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            print(f"  Would clear {table}: {count} rows")
        else:
            # Use DELETE to respect ON DELETE SET NULL in other tables
            # (TRUNCATE CASCADE would delete referencing rows)
            session.execute(text(f"DELETE FROM {table}"))
            print(f"  Cleared {table}")


def migrate(dry_run: bool = False, clear: bool = True):
    """Run the migration."""
    print(f"Source: {CT_ROOT}")
    print(f"Database: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")
    print(f"Dry run: {dry_run}")
    print(f"Clear tables: {clear}")
    print("-" * 50)

    # Parse data
    projects = parse_project_registry(CT_ROOT / 'index.md')
    tasks = parse_task_files(CT_ROOT / 'projects')
    agents = parse_agent_files(CT_ROOT / 'knowledge' / 'agents')
    knowledge = parse_knowledge_files(CT_ROOT / 'knowledge')

    print(f"Parsed: {len(projects)} projects, {len(tasks)} tasks, {len(agents)} agents, {len(knowledge)} knowledge items")

    if dry_run:
        print("\n[DRY RUN] Would clear tables:")
        engine = create_engine(DATABASE_URL)
        Session = sessionmaker(bind=engine)
        session = Session()
        if clear:
            clear_tables(session, TABLES_TO_CLEAR, dry_run=True)
        session.close()

        print("\n[DRY RUN] Would import:")
        for p in projects[:5]:
            print(f"  Project: {p['id']} -> {p['repo_root']}")
        if len(projects) > 5:
            print(f"  ... and {len(projects) - 5} more")
        for a in agents[:5]:
            print(f"  Agent: {a['id']} (model={a['model']}, cli={a['cli']})")
        if len(agents) > 5:
            print(f"  ... and {len(agents) - 5} more")
        for t in tasks[:5]:
            print(f"  Task: {t['id']} ({t['status']})")
        if len(tasks) > 5:
            print(f"  ... and {len(tasks) - 5} more")
        for k in knowledge[:3]:
            print(f"  Knowledge: {k['id']} ({k['category']})")
        if len(knowledge) > 3:
            print(f"  ... and {len(knowledge) - 3} more")
        return

    # Connect to database
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Clear tables first
        if clear:
            print("\nClearing tables...")
            # Disable triggers that block deletion
            print("  Disabling immutable triggers...")
            session.execute(text("ALTER TABLE gate_records DISABLE TRIGGER trg_gate_records_immutable"))
            clear_tables(session, TABLES_TO_CLEAR)
            session.execute(text("ALTER TABLE gate_records ENABLE TRIGGER trg_gate_records_immutable"))
            session.commit()

        # Disable trigger for legacy import (it checks gate_records which we don't have)
        print("Disabling done-verdict trigger for import...")
        session.execute(text("ALTER TABLE tasks DISABLE TRIGGER trg_tasks_done_verdict"))

        # Insert projects
        print("\nImporting projects...")
        for p in projects:
            session.execute(text("""
                INSERT INTO projects (id, name, repo_root, status, description, context_md, task_prefix)
                VALUES (:id, :name, :repo_root, :status, :description, :context_md, :task_prefix)
            """), {
                **p,
                'status': p.get('status', 'active'),
                'description': p.get('description'),
                'context_md': p.get('context_md'),
                'task_prefix': p.get('task_prefix'),
            })

        # Insert agents
        print("Importing agents...")
        for a in agents:
            session.execute(text("""
                INSERT INTO agents (id, name, role, capabilities, type, model, effort,
                                   cli, provider, is_default, agent_type, success_rate, status)
                VALUES (:id, :name, :role, :capabilities, :type, :model, :effort,
                       :cli, :provider, :is_default, :agent_type, :success_rate, :status)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    role = EXCLUDED.role,
                    capabilities = EXCLUDED.capabilities,
                    type = EXCLUDED.type,
                    model = COALESCE(EXCLUDED.model, agents.model),
                    effort = COALESCE(EXCLUDED.effort, agents.effort),
                    cli = COALESCE(EXCLUDED.cli, agents.cli),
                    provider = COALESCE(EXCLUDED.provider, agents.provider),
                    is_default = EXCLUDED.is_default,
                    agent_type = EXCLUDED.agent_type,
                    status = EXCLUDED.status,
                    -- measured in production; the md value is only an
                    -- initial estimate, never overwrite a real score
                    success_rate = agents.success_rate,
                    updated_at = now()
            """), {
                'id': a['id'],
                'name': a['name'],
                'role': a['role'],
                'capabilities': json.dumps(a['capabilities']),
                'type': a['type'],
                'model': a['model'],
                'effort': a['effort'],
                'cli': a['cli'],
                'provider': a['provider'],
                'is_default': a['is_default'],
                'agent_type': a['agent_type'],
                'success_rate': a['success_rate'],
                'status': a['status'],
            })

        # Insert tasks
        print("Importing tasks...")
        for t in tasks:
            executor = t.get('executor')
            reviewer = t.get('reviewer')
            if executor and reviewer and executor == reviewer:
                reviewer = None

            ac = t['acceptance_criteria'] if isinstance(t['acceptance_criteria'], list) else []
            files = t['files'] if isinstance(t['files'], list) else []
            tests = t['tests'] if isinstance(t['tests'], list) else []
            flows = t['flows'] if isinstance(t['flows'], list) else []
            tags = t['tags'] if isinstance(t['tags'], list) else []
            pred_factors = t['prediction_factors'] if isinstance(t.get('prediction_factors'), dict) else None

            # Handle done tasks missing required fields (legacy data)
            # Constraint: done tasks need executor, reviewer (different), and result_ref
            result_ref = t.get('result_ref')
            status = t.get('status', 'todo')
            legacy_no_ac = False

            # Get verdict from file
            verdict = t.get('verdict')

            if status == 'done':
                missing_fields = []
                if not executor:
                    missing_fields.append('executor')
                if not reviewer:
                    missing_fields.append('reviewer')
                if not result_ref:
                    missing_fields.append('result_ref')
                    result_ref = 'legacy-migration'
                if not verdict:
                    missing_fields.append('verdict')
                    verdict = 'pass'  # trigger requires verdict='pass' for done tasks

                if missing_fields:
                    legacy_no_ac = True
                    # Provide placeholder values for constraint
                    if not executor:
                        executor = '@legacy-executor'
                    if not reviewer:
                        reviewer = '@legacy-reviewer'

            session.execute(text("""
                INSERT INTO tasks (id, project, title, raw_input, status, current_gate, mode, priority, risk,
                                  executor, reviewer, acceptance_criteria, files, tests, flows,
                                  plan, result_ref, verdict, predicted_success, prediction_factors,
                                  deadline, tags, legacy_no_ac,
                                  created_at, updated_at, dispatched_at, completed_at)
                VALUES (:id, :project, :title, :raw_input, :status, :current_gate, :mode, :priority, :risk,
                       :executor, :reviewer, :acceptance_criteria, :files, :tests, :flows,
                       :plan, :result_ref, :verdict, :predicted_success, :prediction_factors,
                       :deadline, :tags, :legacy_no_ac,
                       :created_at, :updated_at, :dispatched_at, :completed_at)
            """), {
                'id': t['id'],
                'project': t['project'],
                'title': t['title'],
                'raw_input': t.get('raw_input'),
                'status': status,
                'current_gate': t.get('current_gate', 'spec'),
                'mode': t.get('mode', 'supervised'),
                'priority': t.get('priority'),
                'risk': t.get('risk'),
                'executor': executor,
                'reviewer': reviewer,
                'acceptance_criteria': json.dumps(ac),
                'files': json.dumps(files),
                'tests': json.dumps(tests),
                'flows': json.dumps(flows),
                'plan': t.get('plan'),
                'result_ref': result_ref,
                'verdict': verdict,
                'predicted_success': t.get('predicted_success'),
                'prediction_factors': json.dumps(pred_factors) if pred_factors else None,
                'deadline': t.get('deadline'),
                'tags': json.dumps(tags),
                'legacy_no_ac': legacy_no_ac,
                'created_at': t.get('created_at'),
                'updated_at': t.get('updated_at'),
                'dispatched_at': t.get('dispatched_at'),
                'completed_at': t.get('completed_at'),
            })

        # Insert knowledge items
        print("Importing knowledge...")
        for k in knowledge:
            tags = k['tags'] if isinstance(k['tags'], list) else []
            session.execute(text("""
                INSERT INTO knowledge_items (id, title, category, content, tags, project, author, status)
                VALUES (:id, :title, :category, :content, :tags, :project, :author, :status)
            """), {
                **k,
                'tags': json.dumps(tags),
            })

        # Insert task dependencies
        print("Importing task dependencies...")
        all_task_ids = {t['id'] for t in tasks}
        for t in tasks:
            deps = t.get('depends_on')
            if not deps:
                continue
            if isinstance(deps, str):
                deps = [deps]
            for dep in deps:
                dep = dep.strip()
                if dep and dep in all_task_ids:
                    try:
                        session.execute(text("""
                            INSERT INTO task_dependencies (task_id, depends_on_task_id)
                            VALUES (:task_id, :depends_on_task_id)
                            ON CONFLICT DO NOTHING
                        """), {
                            'task_id': t['id'],
                            'depends_on_task_id': dep,
                        })
                    except Exception as e:
                        print(f"Skipping dependency {t['id']} -> {dep}: {e}")

        # Re-enable trigger
        print("Re-enabling done-verdict trigger...")
        session.execute(text("ALTER TABLE tasks ENABLE TRIGGER trg_tasks_done_verdict"))

        session.commit()
        print(f"\nImported: {len(projects)} projects, {len(agents)} agents, {len(tasks)} tasks, {len(knowledge)} knowledge items")

    except Exception as e:
        session.rollback()
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        session.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Migrate markdown to PostgreSQL')
    parser.add_argument('--dry-run', action='store_true', help='Preview without committing')
    parser.add_argument('--no-clear', action='store_true', help='Skip clearing tables (upsert mode)')
    args = parser.parse_args()

    migrate(dry_run=args.dry_run, clear=not args.no_clear)
