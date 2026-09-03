"""Collector worker.

Runs a loop every COLLECT_INTERVAL seconds:
  * system metrics (CPU/RAM/disk/network) via psutil
  * GPU metrics via NVML (pynvml) or the nvidia-smi binary (when available)
  * vLLM metrics (Prometheus /metrics, /version) via httpx
  * service health pings (vLLM, Open WebUI, DB, Redis)
  * request logs from the Open WebUI API (admin token) and/or an access log file
  * hourly pre-aggregation, retention cleanup, threshold alerts (Telegram)

Run:  python -m collector
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import httpx
import psutil
from sqlalchemy import case, delete, func, select, text

import notifier
from config import CFG
from database import SessionLocal, engine as db_engine, init_db
from models import HourlyAgg, MetricSnapshot, RequestLog, ServiceStatus

log = logging.getLogger("monitoring.collector")

# --------------------------------------------------------------------------- GPU
try:
    import pynvml

    pynvml.nvmlInit()
    GPU_COUNT = pynvml.nvmlDeviceGetCount()
    HAS_NVML = GPU_COUNT > 0
except Exception:
    HAS_NVML = False
    GPU_COUNT = 0

NVIDIA_SMI = shutil.which("nvidia-smi")


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_prometheus(raw: str) -> dict:
    """Parse Prometheus exposition text into {metric_name: value} (labels dropped)."""
    values = {}
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0].split("{")[0]
        val = parts[-1]
        try:
            v = float(val)
        except ValueError:
            if len(parts) >= 3:  # trailing unix timestamp
                try:
                    v = float(parts[-2])
                except ValueError:
                    continue
            else:
                continue
        values[name] = v
    return values


def _dig(data, *keys):
    """Fetch a nested value by path; return None when missing."""
    cur = data
    for k in keys:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list):
            try:
                cur = cur[int(k)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _mget(m, *names):
    """First non-None metric value among alternative names.

    vLLM renamed several Prometheus metrics across versions:
      vLLM 0.8+:  vllm:gpu_cache_usage_perc        -> vllm:kv_cache_usage_perc
                  vllm:num_prompt_tokens_processed_total    -> vllm:prompt_tokens_total
                  vllm:num_generation_tokens_processed_total -> vllm:generation_tokens_total
      vLLM 0.14+/0.2x:
                  vllm:time_per_output_token_seconds -> vllm:inter_token_latency_seconds
                  vllm:time_to_first_token_seconds   (only with detailed tracing;
                                                      approximated as e2e - decode)
                  prefix-cache hit rate gauge        -> counters
                  vllm:prefix_cache_hits_total / vllm:prefix_cache_queries_total
    We accept all variants so the collector works on any recent vLLM.
    """
    for n in names:
        v = m.get(n)
        if v is not None:
            return v
    return None


class Collector:
    _TOK = {
        "in": ("vllm:num_prompt_tokens_processed_total", "vllm:prompt_tokens_total"),
        "out": ("vllm:num_generation_tokens_processed_total", "vllm:generation_tokens_total"),
    }

    def __init__(self):
        self.interval = CFG.collect_interval
        self.client = httpx.AsyncClient(timeout=8, headers={"User-Agent": "monitoring-app/1.0"})
        self._prev_sys = None
        self._prev_sys_ts = 0.0
        self._prev_vllm = {}
        self._last_hourly = None
        self._last_cleanup_day = None

    # ------------------------------------------------------------------ system
    def system_snapshot(self) -> dict:
        now = time.time()
        rates = {}
        d_now = n_now = None
        if self._prev_sys is not None:
            dt = max(now - self._prev_sys_ts, 0.001)
            p = self._prev_sys
            try:
                d_now = psutil.disk_io_counters()
            except Exception:
                pass
            try:
                n_now = psutil.net_io_counters()
            except Exception:
                pass
            if d_now and p.get("disk"):
                rates["disk_bps"] = {
                    "read_bps": max(0.0, (d_now.read_bytes - p["disk"].read_bytes) / dt),
                    "write_bps": max(0.0, (d_now.write_bytes - p["disk"].write_bytes) / dt),
                }
            if n_now and p.get("net"):
                rates["net_bps"] = {
                    "rx_bps": max(0.0, (n_now.bytes_recv - p["net"].bytes_recv) / dt),
                    "tx_bps": max(0.0, (n_now.bytes_sent - p["net"].bytes_sent) / dt),
                }
        self._prev_sys = {"disk": d_now, "net": n_now}
        self._prev_sys_ts = now

        try:
            load1, load5, load15 = psutil.getloadavg()
        except (OSError, AttributeError):
            load1 = load5 = load15 = 0.0
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        du = psutil.disk_usage("/")
        dbps = rates.get("disk_bps", {})
        nbps = rates.get("net_bps", {})
        return {
            "cpu": {
                "pct": psutil.cpu_percent(interval=None),
                "per_core": psutil.cpu_percent(percpu=True),
                "load1": load1,
                "load5": load5,
                "load15": load15,
            },
            "ram": {
                "pct": vm.percent,
                "total_mb": round(vm.total / 2**20),
                "used_mb": round(vm.used / 2**20),
                "available_mb": round(vm.available / 2**20),
                "swap_used_mb": round(swap.used / 2**20),
                "swap_total_mb": round(swap.total / 2**20),
            },
            "disk": {
                "read_bps": dbps.get("read_bps"),
                "write_bps": dbps.get("write_bps"),
                "usage_pct": du.percent,
                "used_gb": round(du.used / 2**30, 1),
                "total_gb": round(du.total / 2**30, 1),
            },
            "net": {
                "rx_bps": nbps.get("rx_bps"),
                "tx_bps": nbps.get("tx_bps"),
            },
        }

    # --------------------------------------------------------------------- gpu
    def gpu_snapshot(self):
        gpus = []
        if HAS_NVML:
            for i in range(GPU_COUNT):
                try:
                    h = pynvml.nvmlDeviceGetHandleByIndex(i)
                    util = pynvml.nvmlDeviceGetUtilizationRates(h)
                    mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                    try:
                        temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
                    except Exception:
                        temp = None
                    try:
                        power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
                    except Exception:
                        power = None
                    gpus.append(
                        {
                            "index": i,
                            "name": pynvml.nvmlDeviceGetName(h),
                            "util": util.gpu,
                            "temp": temp,
                            "mem_used_mib": round(mem.used / 2**20),
                            "mem_total_mib": round(mem.total / 2**20),
                            "power_w": round(power, 1) if power is not None else None,
                        }
                    )
                except Exception as exc:
                    log.warning("nvml gpu %s: %s", i, exc)
        elif NVIDIA_SMI:
            try:
                out = subprocess.run(
                    [
                        NVIDIA_SMI,
                        "--query-gpu=index,name,utilization.gpu,temperature.gpu,"
                        "memory.used,memory.total,power.draw",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in out.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 6:
                        gpus.append(
                            {
                                "index": int(parts[0]),
                                "name": parts[1],
                                "util": _to_float(parts[2]),
                                "temp": _to_float(parts[3]),
                                "mem_used_mib": _to_float(parts[4]),
                                "mem_total_mib": _to_float(parts[5]),
                                "power_w": _to_float(parts[6]) if len(parts) > 6 else None,
                            }
                        )
            except Exception as exc:
                log.warning("nvidia-smi failed: %s", exc)
        return gpus or None

    # -------------------------------------------------------------------- vllm
    async def vllm_snapshot(self):
        out = {}
        base = CFG.vllm_url
        try:
            r = await self.client.get(base + "/metrics")
            if r.status_code == 200:
                m = parse_prometheus(r.text)
                out["active"] = m.get("vllm:num_requests_running")
                out["waiting"] = m.get("vllm:num_requests_waiting")
                kv = _mget(m, "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")
                if kv is not None:
                    out["kv_cache_pct"] = round(kv * 100, 2)
                # --- prefix cache: gauge (old vLLM) or hit-rate from counters (0.2x)
                pc = _mget(
                    m,
                    "vllm:gpu_prefix_cache_hit_rate",
                    "vllm:cpu_prefix_cache_hit_rate",
                )
                if pc is not None:
                    out["prefix_hit_pct"] = round(pc * 100, 2)
                else:
                    pc_hits = m.get("vllm:prefix_cache_hits_total")
                    pc_quer = m.get("vllm:prefix_cache_queries_total")
                    if pc_hits is not None and pc_quer is not None:
                        ph = self._prev_vllm.get("pc_h")
                        pq = self._prev_vllm.get("pc_q")
                        if ph is not None and pq is not None:
                            dh, dq = pc_hits - ph, pc_quer - pq
                            if dq > 0 and dh >= 0 and dh <= dq:
                                out["prefix_hit_pct"] = round(dh / dq * 100, 2)
                        self._prev_vllm["pc_h"], self._prev_vllm["pc_q"] = pc_hits, pc_quer
                # --- TTFT: direct metric or approximated as e2e - decode
                ttft_sum, ttft_cnt = (
                    m.get("vllm:time_to_first_token_seconds_sum"),
                    m.get("vllm:time_to_first_token_seconds_count"),
                )
                e2e_sum, e2e_cnt = (
                    m.get("vllm:e2e_request_latency_seconds_sum"),
                    m.get("vllm:e2e_request_latency_seconds_count"),
                )
                dec_sum, dec_cnt = (
                    m.get("vllm:request_decode_time_seconds_sum"),
                    m.get("vllm:request_decode_time_seconds_count"),
                )
                if ttft_cnt:
                    out["ttft_ms"] = round(ttft_sum / ttft_cnt * 1000, 1)
                elif e2e_cnt and dec_cnt:
                    approx = (e2e_sum - dec_sum) / e2e_cnt
                    if approx >= 0:
                        out["ttft_ms"] = round(approx * 1000, 1)
                if e2e_cnt:
                    out["e2e_ms"] = round(e2e_sum / e2e_cnt * 1000)
                # --- TPOT: inter-token latency histogram (0.2x) or legacy metric
                tpot_sum, tpot_cnt = _mget(
                    m,
                    "vllm:inter_token_latency_seconds_sum",
                    "vllm:time_per_output_token_seconds_sum",
                ), _mget(
                    m,
                    "vllm:inter_token_latency_seconds_count",
                    "vllm:time_per_output_token_seconds_count",
                )
                if tpot_cnt:
                    out["tpot_ms"] = round(tpot_sum / tpot_cnt * 1000, 2)
                # --- throughput: counters delta over snapshot interval
                now = time.time()
                if self._prev_vllm.get("ts"):
                    dt = max(now - self._prev_vllm["ts"], 0.001)
                    for k, names in self._TOK.items():
                        old, new = self._prev_vllm.get(k), _mget(m, *names)
                        if old is not None and new is not None and new >= old:
                            out[f"tokens_{k}_s"] = round((new - old) / dt, 1)
                self._prev_vllm = {
                    "in": _mget(m, *self._TOK["in"]),
                    "out": _mget(m, *self._TOK["out"]),
                    "ts": now,
                    "pc_h": m.get("vllm:prefix_cache_hits_total"),
                    "pc_q": m.get("vllm:prefix_cache_queries_total"),
                }
            else:
                out["error"] = f"/metrics -> {r.status_code}"
        except Exception as exc:
            out["error"] = str(exc)
        try:
            r = await self.client.get(base + "/version")
            if r.status_code == 200:
                out["version"] = r.json().get("version")
        except Exception:
            pass
        return out or None

    # --------------------------------------------------------------- services
    async def ping(self, url: str, version_url: str = None) -> dict:
        t0 = time.time()
        up = False
        version = None
        try:
            r = await self.client.get(url)
            up = r.status_code < 500
        except Exception:
            pass
        latency = int((time.time() - t0) * 1000)
        if up and version_url:
            try:
                r = await self.client.get(version_url)
                if r.status_code == 200:
                    version = r.json().get("version")
            except Exception:
                pass
        return {
            "up": up,
            "latency_ms": latency if up else None,
            "version": version,
            "last_ok": now_utc() if up else None,
        }

    def _ping_db(self) -> dict:
        t0 = time.time()
        try:
            with db_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {
                "up": True,
                "latency_ms": int((time.time() - t0) * 1000),
                "version": None,
                "last_ok": now_utc(),
            }
        except Exception:
            return {"up": False, "latency_ms": None, "version": None, "last_ok": None}

    def _ping_redis(self) -> dict:
        from cache import _get

        t0 = time.time()
        try:
            c = _get()
            if c is None:
                raise RuntimeError("unavailable")
            c.ping()
            return {
                "up": True,
                "latency_ms": int((time.time() - t0) * 1000),
                "version": None,
                "last_ok": now_utc(),
            }
        except Exception:
            return {"up": False, "latency_ms": None, "version": None, "last_ok": None}

    # ------------------------------------------------------------------- main
    async def cycle(self):
        session = SessionLocal()
        try:
            sysm = self.system_snapshot()
            gpu = self.gpu_snapshot()
            vllm = await self.vllm_snapshot()
            services = {
                "vllm": await self.ping(
                    CFG.vllm_url + "/v1/models", CFG.vllm_url + "/version"
                ),
                "openwebui": await self.ping(
                    CFG.openwebui_url + "/health", CFG.openwebui_url + "/api/version"
                ),
                "db": self._ping_db(),
                "redis": self._ping_redis(),
            }
            ts = now_utc()
            session.add(
                MetricSnapshot(
                    ts=ts,
                    gpu=gpu,
                    cpu=sysm["cpu"],
                    ram=sysm["ram"],
                    net=sysm["net"],
                    disk=sysm["disk"],
                    vllm=vllm,
                )
            )
            for name, st in services.items():
                row = session.get(ServiceStatus, name)
                if row is None:
                    row = ServiceStatus(name=name)
                    session.add(row)
                row.up = st["up"]
                row.latency_ms = st["latency_ms"]
                if st["version"]:
                    row.version = st["version"]
                if st["last_ok"]:
                    row.last_ok_ts = st["last_ok"]
                row.last_check_ts = ts
            session.commit()
        except Exception:
            session.rollback()
            log.exception("collector cycle failed")
        finally:
            session.close()

        for task, label in (
            (self.poll_openwebui(), "openwebui poll"),
            (self.check_thresholds(), "threshold check"),
        ):
            try:
                await task
            except Exception:
                log.exception("%s failed", label)
        if CFG.log_file:
            try:
                self.parse_log_file()
            except Exception:
                log.exception("log file parse failed")
        try:
            self.run_hourly_agg_if_due()
        except Exception:
            log.exception("hourly aggregation failed")
        try:
            self.cleanup_if_due()
        except Exception:
            log.exception("cleanup failed")

    # ---------------------------------------------------- open webui request logs
    async def poll_openwebui(self):
        if not CFG.openwebui_token:
            return
        headers = {"Authorization": f"Bearer {CFG.openwebui_token}"}
        try:
            r = await self.client.get(
                CFG.openwebui_url + "/api/v1/chats", headers=headers, timeout=10
            )
            if r.status_code != 200:
                return
            chats = r.json()
        except Exception as exc:
            log.debug("openwebui chats poll: %s", exc)
            return
        if not isinstance(chats, list):
            return
        chats.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
        session = SessionLocal()
        added = 0
        try:
            for chat in chats[:10]:
                cid = str(chat.get("id"))
                try:
                    r2 = await self.client.get(
                        f"{CFG.openwebui_url}/api/v1/chats/{cid}",
                        headers=headers,
                        timeout=10,
                    )
                    if r2.status_code != 200:
                        continue
                    messages = r2.json().get("messages") or []
                except Exception:
                    continue
                start = max(0, len(messages) - 30)
                for i in range(start, len(messages)):
                    msg = messages[i]
                    if msg.get("source") != "user":
                        continue
                    nxt = messages[i + 1] if i + 1 < len(messages) else None
                    if not (nxt and nxt.get("source") == "assistant"):
                        nxt = None
                    logrow = self._extract_request(chat, msg, nxt)
                    if logrow is None:
                        continue
                    dup = session.execute(
                        text(
                            "SELECT 1 FROM request_logs "
                            "WHERE source = 'openwebui' AND raw->>'dedup' = :k LIMIT 1"
                        ),
                        {"k": logrow["dedup"]},
                    ).first()
                    if dup:
                        continue
                    session.add(
                        RequestLog(
                            ts=logrow["ts"],
                            source="openwebui",
                            chat_id=cid,
                            user_id=logrow["user"],
                            model=logrow["model"],
                            prompt_preview=logrow["preview"],
                            prompt_tokens=logrow["pt"],
                            completion_tokens=logrow["ct"],
                            latency_ms=logrow["latency"],
                            status=logrow["status"],
                            temperature=logrow["temperature"],
                            raw={"dedup": logrow["dedup"]},
                        )
                    )
                    added += 1
            session.commit()
            if added:
                log.info("openwebui: saved %d new request log(s)", added)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _extract_request(chat: dict, msg: dict, nxt: dict):
        values = msg.get("values") or {}
        nxt_values = (nxt or {}).get("values") or {}
        meta = msg.get("meta") or {}
        chat_meta = meta.get("chat") if isinstance(meta, dict) else None
        chat_meta = chat_meta if isinstance(chat_meta, dict) else {}
        content = str(msg.get("content") or "")
        if not content:
            return None
        create_time = msg.get("create_time") or 0
        ts = (
            datetime.fromtimestamp(create_time / 1000.0, tz=timezone.utc)
            if create_time
            else now_utc()
        )
        model = (
            values.get("model_name")
            or values.get("model")
            or nxt_values.get("model")
            or chat_meta.get("model")
        )
        tokens = nxt_values.get("tokens") or {}
        latency = None
        if nxt and nxt.get("create_time") and create_time:
            latency = int(nxt["create_time"] - create_time)
        dedup = hashlib.sha1(
            f"{chat.get('id')}:{create_time}:{content[:100]}".encode()
        ).hexdigest()
        return {
            "ts": ts,
            "user": str(chat.get("user_id") or "") or None,
            "model": model,
            "preview": content[:500],
            "pt": tokens.get("prompt"),
            "ct": tokens.get("completion"),
            "latency": latency,
            "status": "ok",
            "temperature": values.get("temperature"),
            "dedup": dedup,
        }

    # ------------------------------------------------------------- access logs
    _LOG_RE = re.compile(
        r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] '
        r'"(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" '
        r'(?P<status>\d{3}) (?P<size>\d+|-)'
        r'(?: "[^"]*" "[^"]*")?(?: (?P<rt>\d+(?:\.\d+)?))?$'
    )

    def parse_log_file(self):
        path = CFG.log_file
        if not os.path.isfile(path):
            return
        offsets_path = os.path.join(CFG.app_data_dir, "log_offsets.json")
        offsets = {}
        if os.path.isfile(offsets_path):
            try:
                with open(offsets_path) as f:
                    offsets = json.load(f)
            except Exception:
                offsets = {}
        size = os.path.getsize(path)
        offset = offsets.get(path, 0)
        if size < offset:  # log rotated
            offset = 0
        if size == offset:
            return
        with open(path, errors="replace") as f:
            f.seek(offset)
            new_lines = f.read().splitlines()
        if not new_lines:
            return
        added = 0
        session = SessionLocal()
        try:
            for line in new_lines:
                m = self._LOG_RE.match(line)
                if not m:
                    continue
                path_req = m.group("path")
                if not any(s in path_req for s in ("/chat", "/completions", "/generate")):
                    continue
                status = m.group("status")
                try:
                    ts = datetime.strptime(m.group("ts"), "%d/%b/%Y:%H:%M:%S %z")
                except ValueError:
                    try:
                        ts = datetime.strptime(m.group("ts"), "%d/%b/%Y:%H:%M:%S")
                        ts = ts.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                rt = _to_float(m.group("rt"))
                session.add(
                    RequestLog(
                        ts=ts,
                        source="proxy",
                        chat_id=path_req,
                        user_id=None,
                        ip=m.group("ip"),
                        model=None,
                        prompt_preview=None,
                        latency_ms=int(rt * 1000) if rt else None,
                        status="ok" if int(status) < 400 else "error",
                    )
                )
                added += 1
            session.commit()
            offsets[path] = size
            os.makedirs(CFG.app_data_dir, exist_ok=True)
            with open(offsets_path, "w") as f:
                json.dump(offsets, f)
            if added:
                log.info("access log: saved %d new request(s)", added)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------- thresholds
    _DEFAULT_THRESHOLDS = {
        "gpu_threshold": 90.0,
        "error_rate": 5.0,
        "notifications_enabled": True,
    }

    @staticmethod
    def _settings(session) -> dict:
        from models import Setting

        out = dict(Collector._DEFAULT_THRESHOLDS)
        for r in session.execute(select(Setting)).scalars():
            if r.key in out and r.value is not None:
                out[r.key] = r.value
        return out

    async def check_thresholds(self):
        session = SessionLocal()
        try:
            st = self._settings(session)
            if not st.get("notifications_enabled", True):
                return
            last = (
                session.execute(
                    select(MetricSnapshot).order_by(MetricSnapshot.ts.desc()).limit(1)
                )
                .scalars()
                .first()
            )
            if last is None:
                return
            gpu = _dig(last.gpu, 0, "util")
            if gpu is not None and float(gpu) >= float(st.get("gpu_threshold", 90.0)):
                await notifier.send(
                    f"GPU load {float(gpu):.0f}% — exceeds threshold {st['gpu_threshold']}% "
                    f"(snapshot {last.ts.isoformat()})",
                )
            since = now_utc() - timedelta(hours=1)
            err, tot = session.execute(
                text(
                    "SELECT COUNT(*) FILTER (WHERE status = 'error') AS e, "
                    "COUNT(*) AS t FROM request_logs WHERE ts >= :s"
                ),
                {"s": since},
            ).first()
            tot = int(tot or 0)
            err = int(err or 0)
            if tot >= 10 and (err / tot) * 100.0 >= float(st.get("error_rate", 5.0)):
                await notifier.send(
                    f"Request error rate over the past hour is "
                    f"{err / tot * 100.0:.1f}% ({err}/{tot}) — threshold {st['error_rate']}%",
                )
        finally:
            session.close()


    # ------------------------------------------------------- hourly aggregation
    def run_hourly_agg_if_due(self):
        bucket = now_utc().replace(minute=0, second=0, microsecond=0)
        if self._last_hourly == bucket:
            return
        self._last_hourly = bucket
        since = bucket - timedelta(hours=1)
        session = SessionLocal()
        try:
            rows = (
                session.execute(
                    select(MetricSnapshot)
                    .where(MetricSnapshot.ts >= since)
                    .order_by(MetricSnapshot.ts)
                )
                .scalars()
                .all()
            )
            if not rows:
                return

            def mean(values):
                values = [float(v) for v in values if v is not None]
                return round(sum(values) / len(values), 3) if values else None

            # NB: aliases must not be named e/t - SQLAlchemy 2.0.19+
            # deprecates Row.t/Row.e attributes and the names would clash.
            req = session.execute(
                text(
                    "SELECT COUNT(*) FILTER (WHERE status = 'error') AS err_cnt, "
                    "COUNT(*) AS tot_cnt FROM request_logs WHERE ts >= :s AND ts < :e"
                ),
                {"s": since, "e": bucket},
            ).first()
            row = (
                session.execute(
                    select(HourlyAgg).where(HourlyAgg.hour_bucket == bucket)
                )
                .scalars()
                .first()
            )
            if row is None:
                row = HourlyAgg(hour_bucket=bucket)
                session.add(row)
            row.gpu_util_avg = mean([_dig(r.gpu, 0, "util") for r in rows])
            row.gpu_temp_avg = mean([_dig(r.gpu, 0, "temp") for r in rows])
            row.cpu_pct_avg = mean([_dig(r.cpu, "pct") for r in rows])
            row.ram_pct_avg = mean([_dig(r.ram, "pct") for r in rows])
            row.net_rx_avg = mean([_dig(r.net, "rx_bps") for r in rows])
            row.net_tx_avg = mean([_dig(r.net, "tx_bps") for r in rows])
            row.disk_read_avg = mean([_dig(r.disk, "read_bps") for r in rows])
            row.disk_write_avg = mean([_dig(r.disk, "write_bps") for r in rows])
            row.vllm_active_avg = mean([_dig(r.vllm, "active") for r in rows])
            row.vllm_tokens_in_avg = mean([_dig(r.vllm, "tokens_in_s") for r in rows])
            row.vllm_tokens_out_avg = mean([_dig(r.vllm, "tokens_out_s") for r in rows])
            row.vllm_ttft_avg = mean([_dig(r.vllm, "ttft_ms") for r in rows])
            row.vllm_tpot_avg = mean([_dig(r.vllm, "tpot_ms") for r in rows])
            row.requests = int(req.tot_cnt or 0)
            row.errors = int(req.err_cnt or 0)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ----------------------------------------------------------------- cleanup
    def cleanup_if_due(self):
        today = now_utc().date()
        if self._last_cleanup_day == today:
            return
        self._last_cleanup_day = today
        session = SessionLocal()
        try:
            now = now_utc()
            session.execute(
                delete(MetricSnapshot).where(
                    MetricSnapshot.ts < now - timedelta(days=CFG.snapshot_retention_days)
                )
            )
            session.execute(
                text(
                    "UPDATE request_logs SET prompt_preview = NULL, raw = NULL "
                    "WHERE ts < :s AND prompt_preview IS NOT NULL"
                ),
                {"s": now - timedelta(days=CFG.prompt_retention_days)},
            )
            session.execute(
                delete(RequestLog).where(
                    RequestLog.ts < now - timedelta(days=CFG.prompt_retention_days * 7)
                )
            )
            session.execute(
                delete(HourlyAgg).where(
                    HourlyAgg.hour_bucket < now - timedelta(days=CFG.snapshot_retention_days)
                )
            )
            session.commit()
            log.info("retention cleanup done")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

async def run_forever() -> None:
    """Entry point: standalone worker or in-process task (RUN_COLLECTOR=true)."""
    init_db()
    psutil.cpu_percent()  # prime the first reading
    col = Collector()
    log.info(
        "collector started: interval=%.0fs vllm=%s openwebui=%s gpu(nvml)=%s",
        col.interval,
        CFG.vllm_url,
        CFG.openwebui_url,
        HAS_NVML,
    )
    while True:
        t0 = time.time()
        try:
            await col.cycle()
        except Exception:
            log.exception("collector cycle crashed")
        await asyncio.sleep(max(1.0, col.interval - (time.time() - t0)))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    asyncio.run(run_forever())
