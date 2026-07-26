# Control Tower V2

Control Tower V2 is a redesigned task coordination and management system built with Python, FastAPI, LangGraph, Chainlit, and Streamlit.

## Architecture & Services

The application consists of four main Docker container services:

- **`db`**: PostgreSQL 16 database (Source of Truth)
- **`backend`**: FastAPI REST API + Alembic migrations
- **`chat`**: Chainlit interactive Chat UI
- **`dashboard`**: Streamlit Task Monitoring Dashboard

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (v20.10+)
- [Docker Compose](https://docs.docker.com/compose/) (v2.0+ or `docker-compose`)

## Environment Setup

Copy `.env.example` to `.env` and configure your environment variables:

```bash
cp .env.example .env
```

Key environment variables:
- `POSTGRES_USER`: Database user (default: `ct`)
- `POSTGRES_DB`: Database name (default: `control_tower`)
- `DB_PASSWORD`: Database password
- `POSTGRES_PORT`: Host port mapped for PostgreSQL (default: `15436`)
- `BACKEND_PORT`: Host port mapped for FastAPI backend (default: `18000`)
- `CHAT_PORT`: Host port mapped for Chainlit chat UI (default: `18080`)
- `DASHBOARD_PORT`: Host port mapped for Streamlit dashboard (default: `18501`)
- `ANTHROPIC_API_KEY`: API key for Claude integration

## Deployment

### Using Deployment Script

Run the automated deployment script:

```bash
./scripts/deploy.sh
```

### Using Docker Compose Directly

To build and start all containers in detached mode:

```bash
docker-compose up --build -d
```

or with Docker CLI v2:

```bash
docker compose up --build -d
```

## Service URLs

Once all containers are running and healthy:

- **Backend API & Health**: [http://localhost:18000](http://localhost:18000) (OpenAPI Docs: [http://localhost:18000/docs](http://localhost:18000/docs))
- **Chainlit Chat UI**: [http://localhost:18080](http://localhost:18080)
- **Streamlit Dashboard**: [http://localhost:18501](http://localhost:18501)
- **PostgreSQL**: `localhost:15436`

*(Host ports are configurable in `.env`)*

## Verification & Health Status

Check the status of running containers and health checks:

```bash
docker-compose ps
```

Verify individual service health endpoints:

```bash
# Backend Health Endpoint
curl http://localhost:18000/health

# Chat UI Endpoint
curl http://localhost:18080/

# Dashboard Endpoint
curl http://localhost:18501/_stcore/health
```

## Shutdown & Cleanup

To stop and remove all services without deleting persistent volumes:

```bash
docker-compose down
```

To stop all services and remove database volumes (clean shutdown):

```bash
docker-compose down -v
```
