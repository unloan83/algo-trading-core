import json
import os
import yaml
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.risk_governor import RiskGovernor

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MARKER_DIR = PROJECT_ROOT / ".cache" / "runtime"


def now_ist_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def project_config(filename: str):
    path = os.path.join(os.path.dirname(__file__), "..", "config", filename)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_risk_governor() -> RiskGovernor:
    cfg = project_config("risk_limits.yaml")
    cooldown = cfg["consecutive_loss_cooldown"]
    return RiskGovernor(
        risk_per_trade_pct=float(cfg["risk_per_trade_pct"]),
        max_open_risk_pct=float(cfg["max_concurrent_open_risk_pct"]),
        daily_circuit_pct=float(cfg["daily_loss_circuit_pct"]),
        weekly_circuit_pct=float(cfg["weekly_loss_circuit_pct"]),
        monthly_circuit_pct=float(cfg["monthly_loss_circuit_pct"]),
        max_positions=int(cfg["max_concurrent_positions"]),
        cooldown_losses_trigger=int(cooldown["trigger_after_losses"]),
        cooldown_hours=int(cooldown["cooldown_hours"]),
        no_averaging_down=bool(cfg["no_averaging_down"]),
    )


def save_market_filters(halted, corporate_actions):
    path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".cache", "market_filters.json")
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "date": now_ist_naive().date().isoformat(),
        "halted": sorted(set(halted)),
        "corporate_actions": sorted(set(corporate_actions)),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def load_market_filters():
    path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".cache", "market_filters.json")
    )
    if not os.path.exists(path):
        raise RuntimeError("MARKET_FILTER_CACHE_MISSING_RUN_PREFLIGHT")
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if payload.get("date") != now_ist_naive().date().isoformat():
        raise RuntimeError("MARKET_FILTER_CACHE_STALE_RUN_PREFLIGHT")
    return payload.get("halted", []), payload.get("corporate_actions", [])


def write_runtime_marker(service: str, status: str, message: str = ""):
    safe_service = service.strip().lower().replace("/", "_")
    RUNTIME_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNTIME_MARKER_DIR / f"{safe_service}.json"
    payload = {
        "service": safe_service,
        "status": str(status).upper(),
        "timestamp": now_ist_naive().isoformat(),
        "message": str(message or "")[:500],
    }
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp_path, path)
    return str(path)


def read_runtime_marker(service: str):
    safe_service = service.strip().lower().replace("/", "_")
    path = RUNTIME_MARKER_DIR / f"{safe_service}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        timestamp = datetime.fromisoformat(payload["timestamp"])
        return {
            "service": payload.get("service", safe_service),
            "status": str(payload.get("status", "")).upper(),
            "timestamp": timestamp,
            "message": str(payload.get("message", "")),
        }
    except Exception:
        return None
