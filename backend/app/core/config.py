import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/control_tower"

    # Headroom compression
    HEADROOM_COMPRESSION_ENABLED: bool = False
    HEADROOM_MIN_CHARS: int = 1000

    # Context replay: keep complete tool output only for the most recent turns.
    TOOL_RESULT_REPLAY_TURNS: int = 3

    # Context compaction is based on the active model's context window rather
    # than an arbitrary number of messages.
    COMPACTION_THRESHOLD_RATIO: float = 0.75
    COMPACTION_MODEL: str = "moonshotai/Kimi-K2-Instruct"
    COMPACTION_MAX_OUTPUT_TOKENS: int = 1024

    # Coordinator execution budgets
    COORDINATOR_MAX_TOOL_ITERATIONS: int = 20
    COORDINATOR_MAX_TURN_TOKENS: int = 100_000
    COORDINATOR_MAX_REPEATED_TOOL_CALLS: int = 3

    # Autonomous execution brakes.  The Setting table may override these at
    # runtime; these values are the safe process-start defaults.
    AUTONOMY_ENABLED: bool = True
    MAX_COST_USD_PER_TASK: float = 10.0
    MAX_CONCURRENT_RUNS: int = 2
    RUN_TIMEOUT_SECONDS: int = 900
    MAX_ACTIVE_SECONDS_PER_RUN: int = 3600
    MAX_TOOL_CALLS_PER_RUN: int = 200
    MAX_NO_PROGRESS_SECONDS: int = 300

    # MCP projection (ADR-001 §D5): scoped token the mcp_server.py handlers
    # use to authenticate against POST /api/mcp/tools/call, and the base URL
    # the coordinator chat CLI's MCP subprocess reaches this API on. Empty
    # token means the endpoint stays closed (fail-closed default).
    MCP_API_TOKEN: str = ""
    CT_API_URL: str = "http://localhost:8000"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
