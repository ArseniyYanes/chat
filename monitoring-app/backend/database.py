"""Database engine / session setup."""
import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from config import CFG

log = logging.getLogger("monitoring.db")

engine = create_engine(
    CFG.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    connect_args={"connect_timeout": 10},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()

TIMESCALE = False


def init_db() -> None:
    """Create schema; promote metrics_snapshot to a hypertable when TimescaleDB is present."""
    global TIMESCALE
    import models  # noqa: F401  (register ORM models)

    Base.metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            conn.execute(
                text("SELECT create_hypertable('metrics_snapshot', 'ts', if_not_exists => TRUE)")
            )
        TIMESCALE = True
        log.info("TimescaleDB extension active, metrics_snapshot is a hypertable")
    except Exception as exc:  # plain PostgreSQL — still fine
        TIMESCALE = False
        log.warning("TimescaleDB not available (%s); using plain tables", exc)
