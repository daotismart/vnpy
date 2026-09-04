"""System management metrics: background services, Redis, QuestDB."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from md_bus import (
    contracts_key,
    create_redis_client,
    latest_key,
    md_bus_status,
    recorder_group,
    redis_url,
    tick_stream,
)


HEARTBEAT_PREFIX = "vnpy:sys:heartbeat:"
SERVICE_NAMES = ("md_receiver", "recorder", "web")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _now_ts() -> float:
    return time.time()


def write_service_heartbeat(service: str, extra: dict[str, Any] | None = None) -> None:
    """Publish service heartbeat to Redis (survives across containers)."""
    try:
        # Short timeouts: never block the caller if Redis/DNS stalls.
        client = create_redis_client(socket_timeout=1.0, socket_connect_timeout=1.0)
        payload = {
            "service": service,
            "ts": _now_ts(),
            "iso": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "host": os.uname().nodename if hasattr(os, "uname") else "",
        }
        if extra:
            payload.update(extra)
        import json

        client.setex(f"{HEARTBEAT_PREFIX}{service}", 30, json.dumps(payload, ensure_ascii=False))
        try:
            client.close()
        except Exception:
            pass
    except Exception:
        pass


def _parse_heartbeat(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    import json

    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    ts = float(data.get("ts") or 0)
    age = _now_ts() - ts if ts else None
    data["age_sec"] = None if age is None else round(age, 1)
    data["ok"] = bool(age is not None and age < 25)
    return data


def _process_self_stats() -> dict[str, Any]:
    rss_mb = None
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    rss_kb = float(parts[1])
                    rss_mb = round(rss_kb / 1024.0, 1)
                    break
    except Exception:
        pass
    return {
        "pid": os.getpid(),
        "uptime_sec": None,
        "rss_mb": rss_mb,
        "md_source": (_env("LIVE_MD_SOURCE", "ctp") or "ctp").lower(),
        "skip_md": _env("LIVE_CTP_SKIP_MD", "0"),
    }


def collect_process_status() -> dict[str, Any]:
    # Always refresh web heartbeat when queried.
    write_service_heartbeat(
        "web",
        {
            "role": "web",
            "md_bus": md_bus_status(),
            **_process_self_stats(),
        },
    )
    services: dict[str, Any] = {}
    try:
        client = create_redis_client()
        for name in SERVICE_NAMES:
            raw = client.get(f"{HEARTBEAT_PREFIX}{name}")
            hb = _parse_heartbeat(raw)
            services[name] = hb or {"service": name, "ok": False, "age_sec": None, "error": "no heartbeat"}
    except Exception as exc:
        for name in SERVICE_NAMES:
            services[name] = {"service": name, "ok": False, "error": str(exc)}
    return {
        "services": services,
        "expected": list(SERVICE_NAMES),
        "all_ok": all(bool((services.get(n) or {}).get("ok")) for n in SERVICE_NAMES),
    }


def _redis_info_section(client: Any, section: str) -> dict[str, str]:
    try:
        info = client.info(section)
        return {str(k): str(v) for k, v in (info or {}).items()}
    except Exception as exc:
        return {"error": str(exc)}


def collect_redis_status() -> dict[str, Any]:
    try:
        client = create_redis_client()
        client.ping()
        memory = client.info("memory") or {}
        stats = client.info("stats") or {}
        clients = client.info("clients") or {}
        server = client.info("server") or {}
        stream_name = tick_stream()
        stream_len = 0
        groups: list[dict[str, Any]] = []
        try:
            stream_len = int(client.xlen(stream_name) or 0)
            for g in client.xinfo_groups(stream_name) or []:
                groups.append(
                    {
                        "name": g.get("name"),
                        "consumers": g.get("consumers"),
                        "pending": g.get("pending"),
                        "last_delivered_id": g.get("last-delivered-id"),
                        "entries_read": g.get("entries-read"),
                        "lag": g.get("lag"),
                    }
                )
        except Exception as exc:
            groups = [{"error": str(exc)}]
        used = int(memory.get("used_memory") or 0)
        peak = int(memory.get("used_memory_peak") or 0)
        total = int(memory.get("total_system_memory") or 0)
        return {
            "ok": True,
            "url": redis_url(),
            "redis_version": server.get("redis_version"),
            "uptime_sec": int(server.get("uptime_in_seconds") or 0),
            "connected_clients": int(clients.get("connected_clients") or 0),
            "blocked_clients": int(clients.get("blocked_clients") or 0),
            "used_memory": used,
            "used_memory_human": memory.get("used_memory_human"),
            "used_memory_peak_human": memory.get("used_memory_peak_human"),
            "used_memory_pct": round(100.0 * used / total, 2) if total else None,
            "peak_memory_pct": round(100.0 * peak / total, 2) if total else None,
            "total_system_memory_human": memory.get("total_system_memory_human"),
            "instantaneous_ops_per_sec": int(stats.get("instantaneous_ops_per_sec") or 0),
            "total_commands_processed": int(stats.get("total_commands_processed") or 0),
            "keyspace_hits": int(stats.get("keyspace_hits") or 0),
            "keyspace_misses": int(stats.get("keyspace_misses") or 0),
            "dbsize": int(client.dbsize() or 0),
            "latest_ticks": int(client.hlen(latest_key()) or 0),
            "contracts": int(client.hlen(contracts_key()) or 0),
            "tick_stream": stream_name,
            "tick_stream_len": stream_len,
            "recorder_group": recorder_group(),
            "stream_groups": groups,
        }
    except Exception as exc:
        return {"ok": False, "url": redis_url(), "error": str(exc)}


def _questdb_exec(sql: str) -> dict[str, Any]:
    import urllib.parse
    import urllib.request
    import json

    host = _env("DATABASE_HOST", "127.0.0.1")
    port = _env("DATABASE_HTTP_PORT", "9000")
    url = f"http://{host}:{port}/exec?{urllib.parse.urlencode({'query': sql})}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    cols = [c["name"] for c in (result.get("columns") or [])]
    out = []
    for row in result.get("dataset") or []:
        out.append({cols[i]: row[i] for i in range(len(cols))})
    return out


def collect_questdb_status() -> dict[str, Any]:
    host = _env("DATABASE_HOST", "127.0.0.1")
    http_port = int(_env("DATABASE_HTTP_PORT", "9000") or 9000)
    pg_port = int(_env("DATABASE_PORT", "8812") or 8812)
    try:
        storage_rows = _rows(_questdb_exec("SELECT * FROM table_storage()"))
        tables = []
        total_disk = 0
        total_rows = 0
        for row in storage_rows:
            disk = int(row.get("diskSize") or 0)
            nrows = int(row.get("rowCount") or 0)
            total_disk += disk
            total_rows += nrows
            tables.append(
                {
                    "table": row.get("tableName"),
                    "row_count": nrows,
                    "disk_bytes": disk,
                    "disk_mb": round(disk / (1024 * 1024), 2),
                    "partitions": row.get("partitionCount"),
                    "partition_by": row.get("partitionBy"),
                    "wal": row.get("walEnabled"),
                }
            )
        tick_range = {}
        try:
            tr = _rows(
                _questdb_exec(
                    "SELECT count() c, min(datetime) mn, max(datetime) mx FROM dbtickdata"
                )
            )
            if tr:
                tick_range = {
                    "count": tr[0].get("c"),
                    "min_datetime": tr[0].get("mn"),
                    "max_datetime": tr[0].get("mx"),
                }
        except Exception as exc:
            tick_range = {"error": str(exc)}
        bar_range = {}
        try:
            br = _rows(
                _questdb_exec(
                    "SELECT count() c, min(datetime) mn, max(datetime) mx FROM dbbardata"
                )
            )
            if br:
                bar_range = {
                    "count": br[0].get("c"),
                    "min_datetime": br[0].get("mn"),
                    "max_datetime": br[0].get("mx"),
                }
        except Exception as exc:
            bar_range = {"error": str(exc)}
        # Rough lag vs wall clock for newest tick.
        lag_sec = None
        mx = tick_range.get("max_datetime")
        if mx:
            try:
                text = str(mx).replace("Z", "+00:00")
                dt = datetime.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                lag_sec = round((datetime.now(timezone.utc) - dt).total_seconds(), 1)
            except Exception:
                lag_sec = None
        return {
            "ok": True,
            "host": host,
            "http_port": http_port,
            "pg_port": pg_port,
            "tables": tables,
            "total_disk_bytes": total_disk,
            "total_disk_mb": round(total_disk / (1024 * 1024), 2),
            "total_rows": total_rows,
            "tick": tick_range,
            "bar": bar_range,
            "tick_lag_sec": lag_sec,
        }
    except Exception as exc:
        return {
            "ok": False,
            "host": host,
            "http_port": http_port,
            "pg_port": pg_port,
            "error": str(exc),
        }


def collect_system_overview() -> dict[str, Any]:
    processes = collect_process_status()
    redis_st = collect_redis_status()
    qdb = collect_questdb_status()
    alerts: list[str] = []
    for name, hb in (processes.get("services") or {}).items():
        if not hb.get("ok"):
            alerts.append(f"{name} 心跳异常/缺失")
    if not redis_st.get("ok"):
        alerts.append(f"Redis 不可用: {redis_st.get('error')}")
    else:
        pct = redis_st.get("used_memory_pct")
        if pct is not None and pct >= 80:
            alerts.append(f"Redis 内存占用偏高 {pct}%")
        for g in redis_st.get("stream_groups") or []:
            pending = g.get("pending")
            lag = g.get("lag")
            if isinstance(pending, int) and pending > 1000:
                alerts.append(f"Stream 组 {g.get('name')} pending={pending}")
            if isinstance(lag, int) and lag > 5000:
                alerts.append(f"Stream 组 {g.get('name')} lag={lag}")
    if not qdb.get("ok"):
        alerts.append(f"QuestDB 不可用: {qdb.get('error')}")
    else:
        tick_lag = qdb.get("tick_lag_sec")
        # Outside session large lag is normal; only alert if session-ish and lag huge.
        if isinstance(tick_lag, (int, float)) and 0 <= tick_lag < 86400 and tick_lag > 300:
            # Soft alert: data older than 5 minutes (may be night session)
            hhmm = datetime.now().hour * 100 + datetime.now().minute
            if (900 <= hhmm <= 1135) or (1255 <= hhmm <= 1515):
                alerts.append(f"QuestDB 最新 Tick 滞后 {tick_lag}s")
    return {
        "ts": _now_ts(),
        "iso": datetime.now().isoformat(timespec="seconds"),
        "ok": len(alerts) == 0,
        "alerts": alerts,
        "processes": processes,
        "redis": redis_st,
        "questdb": qdb,
        "md_bus": md_bus_status(),
    }
