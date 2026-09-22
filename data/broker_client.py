import gzip
import io
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)

NSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
SUSPENDED_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/suspended-instrument.json.gz"
UPSTOX_BASE = "https://api.upstox.com"

logger = logging.getLogger(__name__)


class UnifiedBrokerClient:
    """
    Read-only Upstox market-data client used by the 30-day PAPER phase.

    No order endpoint is implemented here.
    UPSTOX_ANALYTICS_TOKEN is preferred because it is read-only and long-lived.
    """

    def __init__(self, paper_mode: bool = True, timeout: int = 15):
        if not paper_mode:
            raise RuntimeError("LIVE_TRADING_DISABLED_DURING_30_DAY_PAPER_GATE")
        self.paper_mode = True
        self.timeout = timeout
        # Mandatory for the 30-day PAPER phase: use the read-only
        # Analytics Token only; never fall back to a trading OAuth token.
        self.token = os.getenv("UPSTOX_ANALYTICS_TOKEN")
        self.session = requests.Session()
        self._instrument_records: Optional[List[Dict[str, Any]]] = None
        self._instrument_by_symbol: Dict[str, Dict[str, Any]] = {}
        self.cache_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".cache"))
        os.makedirs(self.cache_dir, exist_ok=True)

    def _headers(self) -> Dict[str, str]:
        if not self.token:
            raise RuntimeError("UPSTOX_ANALYTICS_TOKEN_MISSING")
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def _get_json(self, url: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.session.get(
            url,
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") not in (None, "success"):
            raise RuntimeError(f"UPSTOX_API_ERROR: {payload}")
        return payload

    @staticmethod
    def _decode_gzip_json(content: bytes):
        try:
            return json.loads(gzip.decompress(content).decode("utf-8"))
        except OSError:
            return json.loads(content.decode("utf-8"))

    def _load_instruments(self) -> None:
        if self._instrument_records is not None:
            return
        cache_path = os.path.join(self.cache_dir, "upstox_nse_instruments.json")
        today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        records = None
        if os.path.exists(cache_path):
            cache_date = datetime.fromtimestamp(
                os.path.getmtime(cache_path), ZoneInfo("Asia/Kolkata")
            ).date()
            if cache_date == today_ist:
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        records = json.load(f)
                except Exception:
                    records = None
        if records is None:
            resp = self.session.get(NSE_INSTRUMENTS_URL, timeout=self.timeout)
            resp.raise_for_status()
            records = self._decode_gzip_json(resp.content)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(records, f)
        self._instrument_records = records
        self._instrument_by_symbol = {}
        for row in records:
            symbol = str(row.get("trading_symbol", "")).upper()
            segment = row.get("segment")
            instrument_type = row.get("instrument_type")
            if symbol and segment == "NSE_EQ" and instrument_type == "EQ":
                self._instrument_by_symbol[symbol] = row

    def resolve_instrument(self, symbol: str) -> Dict[str, Any]:
        normalized = symbol.strip().upper()
        if normalized in {"NIFTY", "NIFTY50", "NIFTY 50"}:
            return {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "trading_symbol": "NIFTY",
                "segment": "NSE_INDEX",
                "instrument_type": "INDEX",
                "isin": "",
            }
        if normalized in {"TATAMOTORS", "TMPV"}:
            normalized = "TMPV"
        self._load_instruments()
        record = self._instrument_by_symbol.get(normalized)
        if not record:
            raise KeyError(f"UPSTOX_INSTRUMENT_NOT_FOUND:{symbol}")
        return record

    def resolve_instrument_key(self, symbol: str) -> str:
        return str(self.resolve_instrument(symbol)["instrument_key"])

    @staticmethod
    def _candles_to_df(payload: Dict[str, Any]) -> pd.DataFrame:
        candles = payload.get("data", {}).get("candles", []) or []
        rows = []
        for candle in candles:
            if len(candle) < 6:
                continue
            ts = pd.to_datetime(candle[0])
            if ts.tzinfo is not None:
                ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
            rows.append(
                {
                    "timestamp": ts,
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": int(candle[5] or 0),
                }
            )
        if not rows:
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
        return (
            pd.DataFrame(rows)
            .sort_values("timestamp")
            .drop_duplicates("timestamp")
            .reset_index(drop=True)
        )

    def get_historical_data(self, symbol: str, days: int = 210) -> pd.DataFrame:
        """Daily V3 candles. Upstox documents daily availability from Jan-2000."""
        key = quote(self.resolve_instrument_key(symbol), safe="")
        to_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        # Enough calendar buffer for requested trading sessions, kept well below V3's 10y/request cap.
        from_date = to_date - timedelta(days=max(365, int(days * 2.0)))
        url = (
            f"{UPSTOX_BASE}/v3/historical-candle/{key}/days/1/"
            f"{to_date.isoformat()}/{from_date.isoformat()}"
        )
        df = self._candles_to_df(self._get_json(url))
        return df.tail(days).reset_index(drop=True)

    def get_intraday_history(
        self,
        symbol: str,
        interval_minutes: int = 5,
        lookback_days: int = 7,
    ) -> pd.DataFrame:
        """Prior-session history merged with the current intraday session."""
        if not 1 <= interval_minutes <= 300:
            raise ValueError("interval_minutes must be 1..300")
        key = quote(self.resolve_instrument_key(symbol), safe="")
        to_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        from_date = to_date - timedelta(days=max(2, lookback_days))
        historical_url = (
            f"{UPSTOX_BASE}/v3/historical-candle/{key}/minutes/{interval_minutes}/"
            f"{to_date.isoformat()}/{from_date.isoformat()}"
        )
        intraday_url = (
            f"{UPSTOX_BASE}/v3/historical-candle/intraday/{key}/"
            f"minutes/{interval_minutes}"
        )
        historical = self._candles_to_df(self._get_json(historical_url))
        intraday = self._candles_to_df(self._get_json(intraday_url))
        return (
            pd.concat([historical, intraday], ignore_index=True)
            .drop_duplicates("timestamp", keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    def get_ltp(self, symbol: str) -> Optional[float]:
        """
        Return only a genuine Upstox V3 LTP.

        Never fall back to a historical close. Authentication/API failures
        propagate so the paper engine fails closed rather than using stale
        prices for entries, MTM, stops, or targets.
        """
        key = self.resolve_instrument_key(symbol)
        payload = self._get_json(
            f"{UPSTOX_BASE}/v3/market-quote/ltp",
            params={"instrument_key": key},
        )
        data = payload.get("data") or {}
        if not data:
            return None

        first = next(iter(data.values()))
        price = first.get("last_price")
        if price is None:
            return None

        price = float(price)
        return price if price > 0 else None

    def is_nse_trading_day(self, on_date: Optional[date] = None) -> bool:
        d = on_date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
        payload = self._get_json(
            f"{UPSTOX_BASE}/v2/market/timings/{d.isoformat()}"
        )
        for row in payload.get("data", []) or []:
            if row.get("exchange") == "NSE":
                return True
        return False

    def get_market_holidays(self) -> List[Dict[str, Any]]:
        payload = self._get_json(f"{UPSTOX_BASE}/v2/market/holidays")
        return payload.get("data", []) or []

    def validate_readonly_access(self) -> Tuple[bool, str]:
        if not self.token:
            return False, "UPSTOX_ANALYTICS_TOKEN_ERROR:MISSING"

        try:
            key = self.resolve_instrument_key("NIFTY 50")

            price = self.get_ltp("NIFTY 50")
            if not key or price is None or price <= 0:
                return False, "UPSTOX_ANALYTICS_TOKEN_ERROR:LTP_UNAVAILABLE"

            hist = self.get_historical_data("NIFTY 50", days=5)
            if hist.empty or len(hist) < 2:
                return False, "UPSTOX_ANALYTICS_TOKEN_ERROR:HISTORICAL_DATA_UNAVAILABLE"

            return True, "UPSTOX_ANALYTICS_TOKEN_VALID"

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "UNKNOWN"

            if status in (401, 403):
                return False, f"UPSTOX_ANALYTICS_TOKEN_ERROR:HTTP_{status}"

            return False, f"UPSTOX_API_ERROR:HTTP_{status}"

        except Exception as exc:
            return False, f"UPSTOX_API_ERROR:{type(exc).__name__}:{exc}"

    def get_account_health(self) -> Tuple[bool, float, float, str]:
        """
        PAPER-only health. Paper capital is deliberately independent of the live broker balance.
        Runtime scripts recompute free cash/equity from the paper ledger.
        """
        ok, msg = self.validate_readonly_access()
        if not ok:
            return False, 0.0, 0.0, msg
        raw = os.getenv("PAPER_STARTING_CAPITAL", "").strip()
        try:
            capital = float(raw)
        except ValueError:
            capital = 0.0
        if capital <= 0:
            return False, 0.0, 0.0, "PAPER_STARTING_CAPITAL_MISSING_OR_INVALID"
        return True, capital, capital, "PAPER_ACCOUNT_AND_UPSTOX_DATA_OK"

    def get_trading_halts_and_corp_actions(
        self,
        symbols: Optional[List[str]] = None,
        corporate_action_window_days: int = 2,
    ) -> Tuple[List[str], List[str]]:
        """
        Fail-safe filters:
        - suspended NSE symbols from Upstox's suspended instrument file
        - near-term corporate actions from Upstox fundamentals API for the supplied universe
        """
        symbols = symbols or []
        halted: List[str] = []
        corp_actions: List[str] = []

        try:
            suspended_cache = os.path.join(self.cache_dir, "upstox_suspended.json")
            today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
            suspended = None
            if os.path.exists(suspended_cache):
                cache_date = datetime.fromtimestamp(
                    os.path.getmtime(suspended_cache), ZoneInfo("Asia/Kolkata")
                ).date()
                if cache_date == today_ist:
                    try:
                        with open(suspended_cache, "r", encoding="utf-8") as f:
                            suspended = json.load(f)
                    except Exception:
                        suspended = None
            wanted = {s.upper() for s in symbols}
            MAX_PLAUSIBLE_HALT_RATIO = 0.15  # >15% of a liquid universe halted same-day is not credible
            NON_TRADABLE_PLACEHOLDER_TYPES = {"BL", "TL", "DL"}
            for attempt in range(1, 3):
                fetched = suspended is None
                if fetched:
                    resp = self.session.get(SUSPENDED_INSTRUMENTS_URL, timeout=self.timeout)
                    resp.raise_for_status()
                    suspended = self._decode_gzip_json(resp.content)

                # A symbol is halted only when every row for it is an
                # administrative placeholder. Live inspection on 2026-09-22
                # found 3,578/3,579 placeholder symbols also had a normal row.
                symbols_with_live_row = {
                    str(r.get("trading_symbol", "")).upper()
                    for r in suspended
                    if r.get("instrument_type") not in NON_TRADABLE_PLACEHOLDER_TYPES
                }
                all_symbols_in_feed = {
                    str(r.get("trading_symbol", "")).upper() for r in suspended
                }
                halted = sorted((all_symbols_in_feed - symbols_with_live_row) & wanted)

                if not wanted or len(halted) / len(wanted) <= MAX_PLAUSIBLE_HALT_RATIO:
                    if fetched:
                        with open(suspended_cache, "w", encoding="utf-8") as f:
                            json.dump(suspended, f)
                    break

                logger.error(
                    "SUSPENDED_INSTRUMENT_DATA_IMPLAUSIBLE: %d/%d universe symbols matched as "
                    "halted (%.0f%%) — treating upstream suspended-instrument data as unreliable, "
                    "not as %d genuine halts. Sample matched symbols: %s",
                    len(halted), len(wanted), 100 * len(halted) / len(wanted),
                    len(halted), sorted(halted)[:10],
                )
                if attempt == 2:
                    raise RuntimeError("SUSPENDED_INSTRUMENT_DATA_IMPLAUSIBLE")

                delay_seconds = 30 * (2 ** (attempt - 1))
                logger.warning(
                    "Retrying suspended-instrument feed in %d seconds (attempt %d/2)",
                    delay_seconds,
                    attempt + 1,
                )
                time.sleep(delay_seconds)
                suspended = None
        except RuntimeError as exc:
            if str(exc) == "SUSPENDED_INSTRUMENT_DATA_IMPLAUSIBLE":
                raise
            logger.error("Suspended-instrument lookup failed: %s: %s", type(exc).__name__, exc)
            halted = []
        except Exception:
            # Do not turn a non-critical suspended-file outage into fake information.
            halted = []

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        end = today + timedelta(days=corporate_action_window_days)
        for symbol in symbols:
            try:
                record = self.resolve_instrument(symbol)
                isin = record.get("isin")
                if not isin:
                    continue
                payload = self._get_json(
                    f"{UPSTOX_BASE}/v2/fundamentals/{quote(str(isin), safe='')}/corporate-actions"
                )
                for event in payload.get("data", []) or []:
                    raw_date = event.get("expiry_date")
                    if not raw_date:
                        continue
                    event_date = datetime.strptime(raw_date, "%d %b %Y").date()
                    if today <= event_date <= end:
                        corp_actions.append(symbol)
                        break
            except Exception:
                # Corporate-action API failure is handled separately by preflight/logging;
                # never fabricate an action.
                continue

        return sorted(set(halted)), sorted(set(corp_actions))
