import pytest

from app.services.crypto import decrypt_api_key, encrypt_api_key
from app.db.models import Agent, AgentCapability, AgentCapabilityLink, AgentRole, AgentRoleLink
from app.schemas.agent import Agent as AgentSchema
from app.services.entity_admin import EntityValidationError, create_agent, update_agent


def test_agent_admin_normalizes_multiple_roles_and_capabilities(db_session):
    agent = create_agent(
        db_session,
        {
            "id": "@normalized",
            "name": "Normalized",
            "roles": ["executor", "reviewer"],
            "capabilities": ["code", "testing", "code"],
            "cli": "codex",
        },
    )

    assert agent.role == "executor"
    assert agent.capabilities == ["code", "testing"]
    assert agent.normalized_roles == ["executor", "reviewer"]
    assert agent.normalized_capabilities == ["code", "testing"]
    assert {link.role for link in agent.agent_roles} == {AgentRole.EXECUTOR, AgentRole.REVIEWER}
    assert {link.capability for link in agent.agent_capabilities} == {
        AgentCapability.CODE,
        AgentCapability.TESTING,
    }


def test_agent_admin_create_rejects_unknown_model_for_cli(db_session):
    with pytest.raises(EntityValidationError, match="not valid for CLI"):
        create_agent(
            db_session,
            {
                "id": "@bad-model",
                "name": "Bad Model",
                "role": "executor",
                "cli": "agy",
                "model": "gemini-3.6-lite-high",
            },
        )
    assert db_session.query(Agent).filter(Agent.id == "@bad-model").first() is None


def test_agent_admin_create_accepts_known_model_for_cli(db_session):
    agent = create_agent(
        db_session,
        {
            "id": "@good-model",
            "name": "Good Model",
            "role": "executor",
            "cli": "agy",
            "model": "gemini-3.6-flash-high",
        },
    )
    assert agent.model == "gemini-3.6-flash-high"


def test_agent_admin_update_rejects_unknown_model_for_cli(db_session):
    create_agent(
        db_session,
        {
            "id": "@update-model",
            "name": "Update Model",
            "role": "executor",
            "cli": "agy",
            "model": "gemini-3.6-flash-high",
        },
    )
    with pytest.raises(EntityValidationError, match="not valid for CLI"):
        update_agent(db_session, "@update-model", {"model": "not-a-real-model"})


def test_agent_admin_update_ignores_preexisting_invalid_model(db_session):
    agent = create_agent(
        db_session,
        {
            "id": "@legacy-bad-model",
            "name": "Legacy Bad Model",
            "role": "executor",
            "cli": "agy",
            "model": "gemini-3.6-flash-high",
        },
    )
    # Simulate a row that predates the validation (bad model already in DB).
    agent.model = "gemini-3.6-lite-high"
    db_session.commit()

    updated = update_agent(db_session, "@legacy-bad-model", {"status": "disabled"})
    assert updated.status == "disabled"
    assert updated.model == "gemini-3.6-lite-high"


def test_agent_admin_update_syncs_junction_tables(db_session):
    create_agent(
        db_session,
        {
            "id": "@sync",
            "name": "Sync",
            "role": "executor",
            "capabilities": ["code"],
            "cli": "codex",
        },
    )
    agent = update_agent(
        db_session,
        "@sync",
        {"roles": ["coordinator", "spec_plan"], "capabilities": ["architecture"]},
    )

    assert agent.role == "coordinator"
    assert agent.normalized_roles == ["coordinator", "spec_plan"]
    assert agent.normalized_capabilities == ["architecture"]


def test_agent_schema_serializes_normalized_relationships(db_session):
    agent = Agent(
        id="@schema-agent",
        name="Schema Agent",
        role="executor",
        capabilities=["code"],
        status="idle",
    )
    db_session.add(agent)
    db_session.flush()
    agent.agent_roles.append(AgentRoleLink(role=AgentRole.REVIEWER))
    agent.agent_capabilities.append(
        AgentCapabilityLink(capability=AgentCapability.TESTING)
    )
    db_session.commit()

    output = AgentSchema.model_validate(agent)
    assert output.roles == ["reviewer"]
    assert output.capabilities == ["testing"]


def test_api_key_encrypts_and_decrypts_without_plaintext_storage():
    api_key = "sk-test-agent-key"
    encrypted = encrypt_api_key(api_key)

    assert encrypted != api_key
    assert decrypt_api_key(encrypted) == api_key


def test_encryption_produces_distinct_ciphertext():
    assert encrypt_api_key("same-key") != encrypt_api_key("same-key")
