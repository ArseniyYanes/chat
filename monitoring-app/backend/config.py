"""Application configuration from environment variables."""
import os


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # Database / cache
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg2://monitoring:monitoring@db:5432/monitoring"
    )
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")

    # Monitored services
    vllm_url: str = os.getenv("VLLM_API_URL", "http://172.17.0.1:8000").rstrip("/")
    openwebui_url: str = os.getenv("OPEN_WEBUI_URL", "http://172.17.0.1:8080").rstrip("/")
    openwebui_token: str = os.getenv("OPEN_WEBUI_API_TOKEN", "")

    # Collector
    collect_interval: float = float(os.getenv("COLLECT_INTERVAL", "10"))
    prompt_retention_days: int = int(os.getenv("PROMPT_RETENTION_DAYS", "7"))
    snapshot_retention_days: int = int(os.getenv("SNAPSHOTS_RETENTION_DAYS", "30"))
    log_file: str = os.getenv("LOG_FILE", "")  # nginx/OWUI access log (optional)

    # Auth
    admin_user: str = os.getenv("ADMIN_USER", "admin")
    admin_password: str = os.getenv("MONITORING_PASSWORD", "admin")

    # App
    app_data_dir: str = os.getenv("APP_DATA_DIR", "/app/data")
    run_collector: bool = _bool("RUN_COLLECTOR", False)
    port: int = int(os.getenv("PORT", "3000"))
    version: str = os.getenv("APP_VERSION", "1.0.0")

    # Notifications
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


CFG = Config()
