"""ORM models."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

from database import Base

TS = TIMESTAMP(timezone=True)


def _uuid() -> str:
    """Generate a UUIDv4 string (portable, no DB extension required)."""
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MetricSnapshot(Base):
    """One collector tick: system + GPU + vLLM metrics."""

    __tablename__ = "metrics_snapshot"

    ts = Column(TS, primary_key=True)
    gpu = Column(JSONB, nullable=True)      # [{index, name, util, temp, mem_used_mib, mem_total_mib, power_w}]
    cpu = Column(JSONB, nullable=True)      # {pct, per_core[], load1, load5, load15}
    ram = Column(JSONB, nullable=True)      # {pct, total_mb, used_mb, available_mb, swap_used_mb, swap_total_mb}
    net = Column(JSONB, nullable=True)      # {rx_bps, tx_bps}
    disk = Column(JSONB, nullable=True)     # {read_bps, write_bps, usage_pct, used_gb, total_gb}
    vllm = Column(JSONB, nullable=True)     # {active, waiting, kv_cache_pct, prefix_hit_pct, tokens_in_s, tokens_out_s, ttft_ms, tpot_ms, e2e_ms, version}


class RequestLog(Base):
    """A single user request (from Open WebUI API, access log or dashboard test)."""

    __tablename__ = "request_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(TS, index=True)
    source = Column(String(32), default="unknown")   # openwebui | proxy | dashboard-test
    chat_id = Column(String(128), nullable=True)
    user_id = Column(String(128), index=True)
    ip = Column(String(64), nullable=True)
    model = Column(String(256), index=True)
    prompt_preview = Column(Text, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    status = Column(String(16), default="ok")        # ok | error
    temperature = Column(Float, nullable=True)
    max_tokens = Column(Integer, nullable=True)
    raw = Column(JSONB, nullable=True)            # {dedup: ...} etc.


class HourlyAgg(Base):
    """Hourly pre-aggregation for fast history queries."""

    __tablename__ = "hourly_agg"

    hour_bucket = Column(TS, primary_key=True)
    gpu_util_avg = Column(Float)
    gpu_temp_avg = Column(Float)
    cpu_pct_avg = Column(Float)
    ram_pct_avg = Column(Float)
    net_rx_avg = Column(Float)
    net_tx_avg = Column(Float)
    disk_read_avg = Column(Float)
    disk_write_avg = Column(Float)
    vllm_active_avg = Column(Float)
    vllm_tokens_in_avg = Column(Float)
    vllm_tokens_out_avg = Column(Float)
    vllm_ttft_avg = Column(Float)
    vllm_tpot_avg = Column(Float)
    requests = Column(Integer, default=0)
    errors = Column(Integer, default=0)


class ServiceStatus(Base):
    """Last known state of each monitored service."""

    __tablename__ = "service_status"

    name = Column(String(64), primary_key=True)
    up = Column(Boolean, default=False)
    latency_ms = Column(Integer, nullable=True)
    version = Column(String(128), nullable=True)
    last_ok_ts = Column(TS, nullable=True)
    last_check_ts = Column(TS, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(64), primary_key=True)
    value = Column(JSONB)


class AdminAction(Base):
    __tablename__ = "admin_actions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(TS, index=True)
    user = Column(String(128))
    action = Column(String(128))
    details = Column(JSONB, nullable=True)


class ApiKey(Base):
    """A user-facing API key (only the SHA256 hash is stored)."""

    __tablename__ = "api_keys"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(255), nullable=False)
    key_hash = Column(String(255), unique=True, nullable=False, index=True)
    prefix = Column(String(20), nullable=True)         # first 6 chars, for display
    created_at = Column(TS, default=_now)
    last_used_at = Column(TS, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    rate_limit = Column(Integer, default=60)           # requests / minute
    daily_token_limit = Column(Integer, default=1000000)  # tokens / day
    total_requests = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    total_latency_ms = Column(Integer, default=0)  # sum of per-request durations


class ApiUsageLog(Base):
    """One proxied vLLM request, attributed to an API key."""

    __tablename__ = "api_usage_logs"

    id = Column(String(36), primary_key=True, default=_uuid)
    api_key_id = Column(
        String(36), ForeignKey("api_keys.id", ondelete="CASCADE"), index=True
    )
    request_time = Column(TS, index=True, default=_now)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    endpoint = Column(String(255), nullable=True)
    status_code = Column(Integer, nullable=True)
    ip_address = Column(String(45), nullable=True)
    latency_ms = Column(Integer, nullable=True)  # end-to-end proxy duration
