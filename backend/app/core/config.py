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
    MAX_TOKENS_PER_TASK: int = 20_000_000
    MAX_CONCURRENT_RUNS: int = 10
    RUN_TIMEOUT_SECONDS: int = 900
    MAX_ACTIVE_SECONDS_PER_RUN: int = 3600
    MAX_TOOL_CALLS_PER_RUN: int = 200
    MAX_NO_PROGRESS_SECONDS: int = 300

    # Native MCP is the only coordinator/executor transport. Tokens are
    # issued from this secret and validated by app.mcp_native.
    MCP_TOKEN_SECRET: str = ""
    MCP_NATIVE_URL: str = "http://localhost:8100/mcp"
    CT_MCP_PORT: int = 8100

    # Telegram notifications (CTV2-1381)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_NOTIFY_ENABLED: bool = True
    TELEGRAM_TIMEOUT_SECONDS: int = 10
    TELEGRAM_MAX_ATTEMPTS: int = 3
    TELEGRAM_MAX_EVENT_AGE_SECONDS: int = 3600

    # Deadman monitor (CTV2-1400): unfinished work with no progress in this
    # many minutes gets exactly one `deadman` Telegram notification. The
    # Setting table key `deadman_no_progress_minutes` overrides this.
    DEADMAN_NO_PROGRESS_MINUTES: int = 30

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
