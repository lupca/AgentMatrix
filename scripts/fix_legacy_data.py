#!/usr/bin/env python3
"""
Fix legacy data to comply with new DB rules:
1. Auto-assign session_id to tasks with null session_id
2. Insert gate_records (verdict=pass) for done tasks without gate records
3. Parse review MD files and create gate_records from them
"""
import os
import re
import sys
import uuid
import yaml
import hashlib
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


def parse_review_files(projects_dir: Path) -> list[dict]:
    """Parse all review markdown files."""
    reviews = []
    for review_file in projects_dir.glob('*/reviews/*.md'):
        content = review_file.read_text()
        fm = parse_frontmatter(content)
        if not fm.get('id'):
            continue

        task_id = fm.get('id')
        reviews.append({
            'task_id': task_id,
            'executor': fm.get('executor'),
            'reviewer': fm.get('reviewer'),
            'status': fm.get('status'),
            'verdict': fm.get('verdict'),
            'verdict_date': fm.get('verdict_date'),
            'result_ref': fm.get('result_ref'),
            'content': content,
        })

    return reviews


def fix_legacy_data(dry_run: bool = False):
    """Fix legacy data issues."""
    print(f"Database: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")
    print(f"Dry run: {dry_run}")
    print("-" * 50)

    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # 1. Fix tasks with null session_id
        print("\n1. Fixing tasks with null session_id...")
        null_session_tasks = session.execute(text(
            "SELECT id FROM tasks WHERE session_id IS NULL"
        )).fetchall()

        print(f"   Found {len(null_session_tasks)} tasks with null session_id")

        if not dry_run:
            for (task_id,) in null_session_tasks:
                new_session_id = str(uuid.uuid4())
                session.execute(text(
                    "UPDATE tasks SET session_id = :sid WHERE id = :tid"
                ), {'sid': new_session_id, 'tid': task_id})
            print(f"   Assigned session_id to {len(null_session_tasks)} tasks")

        # 2. Find done tasks without verdict gate_records
        print("\n2. Finding done tasks without verdict gate_records...")
        done_without_verdict = session.execute(text("""
            SELECT t.id, t.executor, t.reviewer, t.result_ref, t.mode
            FROM tasks t
            WHERE t.status = 'done'
              AND NOT EXISTS (
                SELECT 1 FROM gate_records gr
                WHERE gr.task_id = t.id
                  AND gr.gate_type = 'verdict'
                  AND gr.status = 'approved'
              )
        """)).fetchall()

        print(f"   Found {len(done_without_verdict)} done tasks without verdict gate_records")

        # 3. Parse review files for additional context
        print("\n3. Parsing review files...")
        reviews = parse_review_files(CT_ROOT / 'projects')
        review_map = {r['task_id']: r for r in reviews}
        print(f"   Found {len(reviews)} review files")

        # 4. Create gate_records for done tasks
        print("\n4. Creating verdict gate_records for done tasks...")
        created = 0
        for task_id, executor, reviewer, result_ref, mode in done_without_verdict:
            # Get review info if exists
            review = review_map.get(task_id, {})

            # Use review data or task data
            final_executor = review.get('executor') or executor or '@legacy-executor'
            final_reviewer = review.get('reviewer') or reviewer or '@legacy-reviewer'
            final_result_ref = review.get('result_ref') or result_ref or 'legacy-migration'
            final_mode = mode or 'supervised'

            # Generate idempotency key
            idem_key = f"legacy-fix-{task_id}-verdict"
            input_hash = hashlib.sha256(f"{task_id}-verdict-pass".encode()).hexdigest()[:64]

            if dry_run:
                print(f"   Would create gate_record for {task_id}: executor={final_executor}, reviewer={final_reviewer}")
            else:
                try:
                    session.execute(text("""
                        INSERT INTO gate_records (
                            task_id, gate_type, status, executor, reviewer,
                            actor, mode, idempotency_key, input_hash, output_ref,
                            output_payload
                        ) VALUES (
                            :task_id, 'verdict', 'approved', :executor, :reviewer,
                            :actor, :mode, :idem_key, :input_hash, :output_ref,
                            '{"verdict": "pass", "source": "legacy-migration"}'::jsonb
                        )
                    """), {
                        'task_id': task_id,
                        'executor': final_executor,
                        'reviewer': final_reviewer,
                        'actor': final_reviewer,
                        'mode': final_mode,
                        'idem_key': idem_key,
                        'input_hash': input_hash,
                        'output_ref': 'pass',
                    })
                    created += 1
                except Exception as e:
                    print(f"   Error creating gate_record for {task_id}: {e}")

        if not dry_run:
            print(f"   Created {created} verdict gate_records")

        # 5. Create review gate_records from review files
        print("\n5. Creating review gate_records from review files...")
        review_created = 0
        for review in reviews:
            task_id = review['task_id']

            # Check if review gate_record already exists
            exists = session.execute(text("""
                SELECT 1 FROM gate_records
                WHERE task_id = :tid AND gate_type = 'review'
            """), {'tid': task_id}).fetchone()

            if exists:
                continue

            # Check if task exists
            task_exists = session.execute(text(
                "SELECT mode FROM tasks WHERE id = :tid"
            ), {'tid': task_id}).fetchone()

            if not task_exists:
                continue

            task_mode = task_exists[0] or 'supervised'
            idem_key = f"legacy-fix-{task_id}-review"
            input_hash = hashlib.sha256(f"{task_id}-review".encode()).hexdigest()[:64]

            if dry_run:
                print(f"   Would create review gate_record for {task_id}")
            else:
                try:
                    verdict_val = review.get("verdict", "pass")
                    session.execute(text("""
                        INSERT INTO gate_records (
                            task_id, gate_type, status, executor, reviewer,
                            actor, mode, idempotency_key, input_hash, output_ref,
                            output_payload
                        ) VALUES (
                            :task_id, 'review', 'approved', :executor, :reviewer,
                            :actor, :mode, :idem_key, :input_hash, :output_ref,
                            jsonb_build_object('verdict', :verdict_val, 'source', 'review-file')
                        )
                    """), {
                        'task_id': task_id,
                        'executor': review.get('executor') or '@legacy-executor',
                        'reviewer': review.get('reviewer') or '@legacy-reviewer',
                        'actor': review.get('reviewer') or '@legacy-reviewer',
                        'mode': task_mode,
                        'idem_key': idem_key,
                        'input_hash': input_hash,
                        'output_ref': review.get('result_ref') or 'review-completed',
                        'verdict_val': verdict_val,
                    })
                    review_created += 1
                except Exception as e:
                    print(f"   Error creating review gate_record for {task_id}: {e}")

        if not dry_run:
            print(f"   Created {review_created} review gate_records")

        if not dry_run:
            session.commit()
            print("\n" + "=" * 50)
            print("DONE! Summary:")
            print(f"  - Session IDs assigned: {len(null_session_tasks)}")
            print(f"  - Verdict gate_records created: {created}")
            print(f"  - Review gate_records created: {review_created}")

    except Exception as e:
        session.rollback()
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        session.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Fix legacy data issues')
    parser.add_argument('--dry-run', action='store_true', help='Preview without committing')
    args = parser.parse_args()

    fix_legacy_data(dry_run=args.dry_run)
