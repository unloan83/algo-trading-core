import csv
import io
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = PROJECT_ROOT / ".cache"
CONSTITUENT_CACHE = CACHE_DIR / "nifty200_constituents.json"
ACTIVE_UNIVERSE_CACHE = CACHE_DIR / "active_universe.json"

DEFAULT_NIFTY200_URL = (
    "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"
)

SYMBOL_ALIASES = {
    "TATAMOTORS": "TMPV",
}


def _now_ist() -> datetime:
    return datetime.now(IST)


def _normalize_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    return SYMBOL_ALIASES.get(normalized, normalized)


def _validated_symbols(symbols: List[str]) -> List[str]:
    unique = []
    seen = set()
    for symbol in symbols:
        normalized = _normalize_symbol(symbol)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    if len(unique) < 180:
        raise RuntimeError(
            f"NIFTY200_CONSTITUENT_SOURCE_INVALID:{len(unique)}_SYMBOLS"
        )
    return unique


def _load_cached_constituents(max_age_days: int) -> List[str]:
    if not CONSTITUENT_CACHE.exists():
        return []
    try:
        payload = json.loads(CONSTITUENT_CACHE.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=IST)
        if _now_ist() - fetched_at > timedelta(days=max_age_days):
            return []
        return _validated_symbols(payload.get("symbols", []))
    except Exception:
        return []


def fetch_nifty200_constituents(
    source_url: str = DEFAULT_NIFTY200_URL,
    max_cache_age_days: int = 30,
    timeout: int = 20,
) -> Tuple[List[str], str]:
    """
    Fetch official NIFTY 200 constituents. If the remote source is temporarily
    unavailable, use only a previously validated recent cache.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/151 Safari/537.36"
        ),
        "Referer": (
            "https://www.niftyindices.com/indices/equity/"
            "broad-based-indices/nifty-200"
        ),
        "Accept": "text/csv,text/plain,*/*",
    }

    try:
        response = requests.get(source_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.text))
        symbols = []
        for row in reader:
            series = str(row.get("Series", "EQ")).strip().upper()
            if series and series != "EQ":
                continue
            symbols.append(row.get("Symbol", ""))
        symbols = _validated_symbols(symbols)
        payload = {
            "fetched_at": _now_ist().isoformat(),
            "source_url": source_url,
            "symbols": symbols,
        }
        CONSTITUENT_CACHE.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        return symbols, "official_niftyindices"
    except Exception as remote_exc:
        cached = _load_cached_constituents(max_cache_age_days)
        if cached:
            return cached, "validated_cached_nifty200"
        raise RuntimeError(
            "NIFTY200_CONSTITUENT_FETCH_FAILED_NO_VALID_CACHE:"
            f"{type(remote_exc).__name__}:{remote_exc}"
        ) from remote_exc


def build_and_cache_dynamic_universe(
    broker,
    universe_cfg: Dict,
) -> Tuple[List[str], Dict]:
    """
    Build one deterministic Top-N universe for the entire trading day.

    Ranking metric: average daily traded value (close * volume) over the
    configured completed-session lookback. Only genuine Upstox data and
    resolvable NSE instruments are eligible.
    """
    mode = str(universe_cfg.get("mode", "")).strip().lower()
    if mode != "dynamic_nifty200_top100":
        raise RuntimeError(f"UNSUPPORTED_UNIVERSE_MODE:{mode}")

    top_n = int(universe_cfg.get("top_n", 100))
    lookback_days = int(universe_cfg.get("adtv_lookback_days", 20))
    min_adtv_cr = float(universe_cfg.get("min_adtv_cr", 50.0))
    max_cache_age_days = int(
        universe_cfg.get("constituent_cache_max_age_days", 30)
    )
    source_url = str(
        universe_cfg.get("source_url", DEFAULT_NIFTY200_URL)
    ).strip()

    if top_n <= 0 or top_n > 200:
        raise RuntimeError(f"INVALID_DYNAMIC_UNIVERSE_TOP_N:{top_n}")
    if lookback_days < 10:
        raise RuntimeError(
            f"INVALID_DYNAMIC_UNIVERSE_LOOKBACK:{lookback_days}"
        )

    constituents, constituent_source = fetch_nifty200_constituents(
        source_url=source_url,
        max_cache_age_days=max_cache_age_days,
    )

    ranked = []
    rejected = []
    fetch_days = max(lookback_days, 25)
    minimum_valid_sessions = max(10, int(lookback_days * 0.75))

    for symbol in constituents:
        try:
            instrument_key = broker.resolve_instrument_key(symbol)
            df = broker.get_historical_data(symbol, days=fetch_days)
            if df.empty:
                rejected.append((symbol, "NO_HISTORY"))
                continue

            tail = df.tail(lookback_days).copy()
            valid = tail[
                (tail["close"] > 0)
                & (tail["volume"] > 0)
            ]
            if len(valid) < minimum_valid_sessions:
                rejected.append(
                    (symbol, f"INSUFFICIENT_VALID_SESSIONS:{len(valid)}")
                )
                continue

            adtv_cr = float(
                (valid["close"] * valid["volume"]).mean() / 10_000_000.0
            )
            if adtv_cr < min_adtv_cr:
                rejected.append(
                    (symbol, f"ADTV_BELOW_MIN:{adtv_cr:.2f}")
                )
                continue

            ranked.append(
                {
                    "symbol": symbol,
                    "instrument_key": instrument_key,
                    "adtv_cr": round(adtv_cr, 2),
                    "valid_sessions": int(len(valid)),
                }
            )
        except Exception as exc:
            rejected.append(
                (symbol, f"{type(exc).__name__}:{exc}")
            )

    ranked.sort(
        key=lambda row: (-float(row["adtv_cr"]), row["symbol"])
    )

    if len(ranked) < top_n:
        raise RuntimeError(
            "DYNAMIC_UNIVERSE_INSUFFICIENT_ELIGIBLE:"
            f"{len(ranked)}/{top_n}"
        )

    selected = ranked[:top_n]
    selected_symbols = [row["symbol"] for row in selected]

    payload = {
        "date": _now_ist().date().isoformat(),
        "generated_at": _now_ist().isoformat(),
        "mode": mode,
        "source": constituent_source,
        "source_url": source_url,
        "top_n": top_n,
        "lookback_days": lookback_days,
        "min_adtv_cr": min_adtv_cr,
        "constituent_count": len(constituents),
        "eligible_count": len(ranked),
        "rejected_count": len(rejected),
        "selected": selected,
    }

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = ACTIVE_UNIVERSE_CACHE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp_path, ACTIVE_UNIVERSE_CACHE)

    return selected_symbols, payload


def load_active_universe(universe_cfg: Dict) -> List[str]:
    """
    Load the Top-N universe produced by today's successful preflight.
    Never silently fall back to an old or static universe.
    """
    if not ACTIVE_UNIVERSE_CACHE.exists():
        raise RuntimeError(
            "ACTIVE_UNIVERSE_CACHE_MISSING_RUN_PREFLIGHT"
        )

    try:
        payload = json.loads(
            ACTIVE_UNIVERSE_CACHE.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise RuntimeError(
            f"ACTIVE_UNIVERSE_CACHE_INVALID:{type(exc).__name__}:{exc}"
        ) from exc

    today = _now_ist().date().isoformat()
    if payload.get("date") != today:
        raise RuntimeError(
            "ACTIVE_UNIVERSE_CACHE_STALE_RUN_PREFLIGHT"
        )

    expected_top_n = int(universe_cfg.get("top_n", 100))
    selected = payload.get("selected", [])
    symbols = [
        _normalize_symbol(row.get("symbol", ""))
        for row in selected
        if row.get("symbol")
    ]

    if len(symbols) != expected_top_n:
        raise RuntimeError(
            "ACTIVE_UNIVERSE_SIZE_MISMATCH:"
            f"{len(symbols)}/{expected_top_n}"
        )

    if len(set(symbols)) != len(symbols):
        raise RuntimeError("ACTIVE_UNIVERSE_DUPLICATE_SYMBOLS")

    return symbols


def load_active_universe_metadata() -> Dict:
    if not ACTIVE_UNIVERSE_CACHE.exists():
        return {}
    try:
        return json.loads(
            ACTIVE_UNIVERSE_CACHE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}
