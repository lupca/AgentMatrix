from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import Agent as AgentModel
from app.schemas.agent import Agent, AgentCreate, AgentUpdate
from app.services.crypto import encrypt_api_key

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _unset_coordinator_defaults(db: Session, except_id: str | None = None) -> None:
    query = db.query(AgentModel).filter(AgentModel.role == "coordinator")
    if except_id:
        query = query.filter(AgentModel.id != except_id)
    query.update({AgentModel.is_default: False}, synchronize_session=False)


def _validate_default_role(role: str, is_default: bool) -> None:
    if is_default and role != "coordinator":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only coordinator agents can be the default coordinator.",
        )


def _validate_agent_configuration(
    agent_type: str,
    cli: str | None,
    provider: str | None,
    has_api_key: bool,
    require_cli: bool = True,
) -> None:
    if agent_type == "api":
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="API agents require a provider.",
            )
        if not has_api_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="API agents require an api_key.",
            )
    elif agent_type == "cli" and require_cli and not cli:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CLI agents require a cli tool.",
        )


@router.get("", response_model=list[Agent])
def get_agents(
    role: str | None = None,
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    query = db.query(AgentModel)
    if role:
        query = query.filter(AgentModel.role == role)
    if status:
        query = query.filter(AgentModel.status == status)
    return query.offset(offset).limit(limit).all()


@router.post("", response_model=Agent, status_code=status.HTTP_201_CREATED)
def create_agent(agent_in: AgentCreate, db: Session = Depends(get_db)):
    existing = db.query(AgentModel).filter(AgentModel.id == agent_in.id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Agent with ID '{agent_in.id}' already exists."
        )

    agent_data = agent_in.model_dump(exclude_unset=True)
    has_explicit_type = "agent_type" in agent_data
    agent_type = agent_data.get("agent_type", "cli")
    api_key = agent_data.pop("api_key", None)
    agent_data["agent_type"] = agent_type
    if "base_url" in agent_data:
        agent_data["base_url"] = (agent_data["base_url"] or "").strip() or None
    _validate_agent_configuration(
        agent_type,
        agent_data.get("cli"),
        agent_data.get("provider"),
        bool(api_key),
        require_cli=has_explicit_type,
    )
    if agent_type == "api":
        agent_data["api_key"] = encrypt_api_key(api_key)
    else:
        agent_data["api_key"] = None
        agent_data["provider"] = None
        agent_data["base_url"] = None
    _validate_default_role(agent_data["role"], agent_data.get("is_default", False))
    if agent_data["role"] != "coordinator":
        agent_data["is_default"] = False
    if agent_data.get("is_default"):
        _unset_coordinator_defaults(db)
    db_agent = AgentModel(**agent_data)
    db.add(db_agent)
    db.commit()
    db.refresh(db_agent)
    return db_agent


@router.get("/{id}", response_model=Agent)
def get_agent(id: str, db: Session = Depends(get_db)):
    db_agent = db.query(AgentModel).filter(AgentModel.id == id).first()
    if not db_agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{id}' not found."
        )
    return db_agent


@router.patch("/{id}", response_model=Agent)
def update_agent(id: str, agent_in: AgentUpdate, db: Session = Depends(get_db)):
    db_agent = db.query(AgentModel).filter(AgentModel.id == id).first()
    if not db_agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{id}' not found."
        )

    update_data = agent_in.model_dump(exclude_unset=True)
    api_key_was_provided = "api_key" in update_data
    api_key = update_data.pop("api_key", None)
    if "base_url" in update_data:
        update_data["base_url"] = (update_data["base_url"] or "").strip() or None
    target_type = update_data.get("agent_type", db_agent.agent_type or "cli")
    target_cli = update_data.get("cli", db_agent.cli)
    target_provider = update_data.get("provider", db_agent.provider)
    has_api_key = bool(api_key) if api_key_was_provided else bool(db_agent.api_key)
    legacy_cli_update = (
        target_type == "cli"
        and not db_agent.cli
        and "agent_type" not in update_data
        and "cli" not in update_data
    )
    _validate_agent_configuration(
        target_type,
        target_cli,
        target_provider,
        has_api_key,
        require_cli=not legacy_cli_update,
    )
    if target_type == "api" and api_key_was_provided:
        update_data["api_key"] = encrypt_api_key(api_key)
    elif target_type == "cli":
        update_data["api_key"] = None
        update_data["provider"] = None
        update_data["base_url"] = None
    target_role = update_data.get("role", db_agent.role)
    target_default = update_data.get("is_default", db_agent.is_default)
    _validate_default_role(target_role, target_default)
    if target_role != "coordinator":
        update_data["is_default"] = False
    if update_data.get("is_default"):
        _unset_coordinator_defaults(db, except_id=id)
    for field, value in update_data.items():
        setattr(db_agent, field, value)

    db.commit()
    db.refresh(db_agent)
    return db_agent


@router.post("/{id}/set-default", response_model=Agent)
def set_default_agent(id: str, db: Session = Depends(get_db)):
    db_agent = db.query(AgentModel).filter(AgentModel.id == id).first()
    if not db_agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{id}' not found.",
        )
    if db_agent.role != "coordinator":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only coordinator agents can be the default coordinator.",
        )

    _unset_coordinator_defaults(db, except_id=id)
    db_agent.is_default = True
    db.commit()
    db.refresh(db_agent)
    return db_agent


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(id: str, db: Session = Depends(get_db)):
    db_agent = db.query(AgentModel).filter(AgentModel.id == id).first()
    if not db_agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{id}' not found."
        )

    db.delete(db_agent)
    db.commit()
    return None
