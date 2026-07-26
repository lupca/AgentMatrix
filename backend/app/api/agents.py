from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import Agent as AgentModel
from app.schemas.agent import Agent, AgentCreate, AgentUpdate

router = APIRouter(prefix="/api/agents", tags=["agents"])


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
    for field, value in update_data.items():
        setattr(db_agent, field, value)

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
