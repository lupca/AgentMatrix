from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import Agent as AgentModel
from app.schemas.agent import Agent, AgentCreate, AgentUpdate

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
