#!/bin/bash
set -e

# Default to running via docker exec against control_tower_db container
if docker ps >/dev/null 2>&1 && docker ps | grep -q control_tower_db; then
    PSQL_CMD="docker exec -i control_tower_db psql -U ct -d control_tower"
else
    # Fallback to local psql if docker is not available or container is not running
    DB_URL=${1:-"postgresql://ct:secret@localhost:5433/control_tower"}
    PSQL_CMD="psql $DB_URL"
fi

$PSQL_CMD -v ON_ERROR_STOP=1 <<-EOSQL
    -- Note: If ct_readonly already exists, this will raise an error, but that's fine or we can ignore it
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ct_readonly') THEN
            CREATE ROLE ct_readonly NOLOGIN;
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ct_readonly_user') THEN
            CREATE ROLE ct_readonly_user LOGIN PASSWORD 'readonly';
        END IF;
    END
    \$\$;

    -- Add the user to the role
    GRANT ct_readonly TO ct_readonly_user;

    -- Grant access to the schema
    GRANT USAGE ON SCHEMA public TO ct_readonly;

    -- Grant read on non-sensitive tables (removed session_events)
    GRANT SELECT ON projects, project_rules, tasks, task_dependencies, task_rounds, dispatch_decisions, dispatch_candidates, task_events, knowledge_items, llm_usage, run_resource_usage, settings, agent_runs, agent_accounts, agent_output_chunks, agent_events, vendor_raw_events, session_event_cursors, audit_log TO ct_readonly;

    -- Grant read on sensitive tables with column exclusions
    -- agents: exclude api_key
    GRANT SELECT (id, name, role, capabilities, status, type, model, effort, cli, agent_type, provider, base_url, is_default, success_rate, created_at, updated_at, archived_at) ON agents TO ct_readonly;

    -- sessions: exclude messages
    GRANT SELECT (id, task_id, project_id, context_level, title, status, pinned, message_count, thread_id, current_gate, checkpoint_id, state_payload, selected_provider, selected_model, created_at, updated_at, last_activity_at, archived_at) ON sessions TO ct_readonly;

    -- admin_gate_records: exclude input_payload, output_payload, error_message
    GRANT SELECT (id, entity, action, entity_id, status, actor, mode, parent_id, created_at, updated_at) ON admin_gate_records TO ct_readonly;

    -- gate_records: exclude input_payload, output_payload, error_message
    GRANT SELECT (id, task_id, gate_type, status, actor, mode, idempotency_key, input_hash, output_ref, parent_id, executor, reviewer, created_at, updated_at) ON gate_records TO ct_readonly;
EOSQL

echo "Role ct_readonly setup complete."
