# Control Tower V2 - LangGraph Redesign

## Overview

This is a complete redesign of the Control Tower task coordination system using LangGraph (Python). The goal is to reduce token consumption by ~80% while maintaining output quality.

## Architecture

- **Backend**: FastAPI + SQLAlchemy + LangGraph
- **Database**: PostgreSQL (source of truth)
- **Chat UI**: Chainlit
- **Dashboard**: Streamlit
- **LLM**: OpenAI-compatible API (coordinator) + account-backed CLIs (`claude`/`agy`/`codex`), direct not through LangChain

## Project Structure

```
control-tower-v2/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI entrypoint
│   │   ├── api/                 # REST endpoints
│   │   ├── db/                  # SQLAlchemy models
│   │   ├── graph/               # LangGraph definitions
│   │   │   ├── state.py         # TaskState Pydantic
│   │   │   ├── nodes.py         # Node functions
│   │   │   ├── builder.py       # StateGraph
│   │   │   └── gates/           # Gate implementations
│   │   ├── schemas/             # Pydantic schemas
│   │   └── services/            # External services (MCP, Claude)
│   ├── tests/
│   ├── alembic/                 # DB migrations
│   └── requirements.txt
├── frontend/
│   ├── chat/                    # Chainlit
│   └── dashboard/               # Streamlit
├── docker-compose.yml
└── .env.example
```

## Commands

```bash
# Development
docker-compose up -d db
cd backend && alembic upgrade head
uvicorn app.main:app --reload

# Chat UI
cd frontend/chat && chainlit run app.py

# Dashboard
cd frontend/dashboard && streamlit run app.py

# Tests
pytest backend/tests/ -v

# Full stack
docker-compose up --build
```

## Key Concepts

### Gates
- **Spec Gate**: Validate input, generate AC (LLM)
- **Plan Gate**: Generate implementation plan (LLM)
- **Dispatch Gate**: Assign executor, change status
- **Review-Order Gate**: Create review sheet
- **Verdict Gate**: Record pass/changes, enforce four-eyes

### Token Optimization
- Only call LLM for judgment tasks (spec validation, plan writing)
- Route commands directly to pipeline (0 tokens)
- Use Haiku for simple tasks, Sonnet for complex ones

### Four-Eyes Rule
- `reviewer` must differ from `executor`
- Enforced at Verdict Gate (hard failure if violated)
