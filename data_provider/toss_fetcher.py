# -*- coding: utf-8 -*-
"""
TossFetcher — KR realtime-primary / US last-resort market data source via Toss Invest
OpenAPI (credential-gated)

Data source: Toss Invest OpenAPI (https://openapi.tossinvest.com)
Auth: OAuth 2.0 Client Credentials Grant (``TOSS_CLIENT_ID`` / ``TOSS_CLIENT_SECRET``)
Markets: KR (bare 6-digit code) + US (ticker as-is)

Capabilities implemented in Phase 1:
- ``get_realtime_quote()`` via ``GET /api/v1/prices`` (batch up to 200 symbols/call,
  10 TPS). The response only carries ``lastPrice``/``symbol``/``currency``/``timestamp``
  — no change/volume/OHLC fields — so quotes are enriched from a small recent-candles
  call (``data_quality="ok"`` when complete, ``"partial"`` if the supplement fails).
- ``get_daily_data()`` via ``GET /api/v1/candles?interval=1d&adjusted=true`` (up to
  200 bars/call, ``before`` cursor pagination, 5 TPS for the chart group).

Constraint: allow-list-only IP registration (Toss WTS > Settings > Open API > Allowed
IP). Calls from an unregistered IP receive HTTP 403; this fetcher surfaces that once
per process via a clear multi-line WARNING (with the caller's public IP and a link to
the registration screen) instead of silently degrading, then skips further Toss calls
for the rest of the process to avoid log spam and needless backoff. See
``docs/adr/0003-toss-openapi-credential-gated-source.md``.

NXT consolidated pricing caveat: both ``get_realtime_quote()`` and ``get_daily_data()``
return prices consolidated across KRX and NXT (Korea's alternative trading venue,
which trades until ~20:00) — the candle endpoint has no session-split parameter.
Measured against the official KRX close on 2026-07-17, this made daily bars diverge
from the standard KRX close by up to 3.8% (both directions), which breaks technical
indicators computed against standard charts. That is why ``TossFetcher`` is NOT the
primary KR *daily* source — it is a fallback behind ``YfinanceFetcher`` (which serves
the official KRX close). For KR *realtime* quotes the same NXT-consolidated latest
trade is the desired behavior (it matches what the Toss app itself shows), so Toss
stays the realtime-primary source there.

Routing: ``DataFetcherManager`` decides real ordering explicitly — KR realtime tries
Toss first with a Yfinance fallback; KR daily has no special routing and falls through
to the manager's generic priority-sorted loop, where ``YfinanceFetcher`` (priority 4)
naturally sorts ahead of ``TossFetcher`` (priority 6); US daily appends Toss at the end
of the existing 4-source chain. ``TossFetcher.priority`` is set to the lowest priority
in the current scheme so it never preempts an existing source in any un-routed,
priority-sorted fallback.
"""

import logging
import os
import random
import threading
import time
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS
from .realtime_types import UnifiedRealtimeQuote, RealtimeSource, safe_float, safe_int
from .us_index_mapping import is_us_stock_code
from src.services.market_symbol_utils import is_suffix_market_symbol, split_suffix_symbol

logger = logging.getLogger(__name__)

_TOSS_BASE_URL = "https://openapi.tossinvest.com"
_TOKEN_URL = f"{_TOSS_BASE_URL}/oauth2/token"
_PRICES_URL = f"{_TOSS_BASE_URL}/api/v1/prices"
_CANDLES_URL = f"{_TOSS_BASE_URL}/api/v1/candles"
_STOCKS_URL = f"{_TOSS_BASE_URL}/api/v1/stocks"
_ACCOUNTS_URL = f"{_TOSS_BASE_URL}/api/v1/accounts"
_HOLDINGS_URL = f"{_TOSS_BASE_URL}/api/v1/holdings"
_ORDERS_URL = f"{_TOSS_BASE_URL}/api/v1/orders"

_TOKEN_EXPIRY_MARGIN_SECONDS = 60  # refresh 60s before actual expiry
_MAX_CANDLES_PER_CALL = 200
_MAX_CANDLE_PAGES = 2  # 2 pages * 200 bars covers the ~250-day indicator window
_MAX_STOCKS_PER_CALL = 200  # GET /api/v1/stocks symbols= batch limit
_STOCKS_BATCH_SLEEP_SECONDS = 0.25  # STOCK rate-limit group is 5 TPS
_RETRY_LIMIT = 2  # 429 retry cap before giving up to the manager's fallback
_IP_LOOKUP_URL = "https://api.ipify.org"
_IP_LOOKUP_TIMEOUT_SECONDS = 5

_ACCOUNT_HEADER = "X-Tossinvest-Account"
_ORDERS_STATUS_CLOSED = "CLOSED"
_ORDERS_PAGE_LIMIT = 100
_ORDERS_PAGE_SLEEP_SECONDS = 0.25  # ORDER_HISTORY rate-limit group is 5 TPS
_ORDERS_MAX_PAGES = 500  # sane runaway guard for user-triggered single-call sync

# Process-wide 403 visibility guard: warn once with actionable detail, then skip
# further Toss calls for the rest of this process (avoids log spam / needless
# retry-backoff once we know the IP isn't allow-listed).
_forbidden_warned = False
_forbidden_lock = threading.Lock()


def is_toss_forbidden() -> bool:
    """Whether a 403 (IP not allow-listed) has already been observed this process."""
    return _forbidden_warned


def reset_toss_forbidden_guard_for_tests() -> None:
    """Reset the process-wide 403 guard. Intended for test isolation only."""
    global _forbidden_warned
    with _forbidden_lock:
        _forbidden_warned = False


def _lookup_public_ip() -> str:
    try:
        resp = requests.get(_IP_LOOKUP_URL, timeout=_IP_LOOKUP_TIMEOUT_SECONDS)
        if resp.ok:
            ip = resp.text.strip()
            if ip:
                return ip
    except Exception:
        pass
    return "확인 불가"


def _mark_forbidden_and_warn(context: str) -> None:
    global _forbidden_warned
    with _forbidden_lock:
        if _forbidden_warned:
            return
        _forbidden_warned = True

    public_ip = _lookup_public_ip()
    logger.warning(
        "[Toss] 허용되지 않은 IP에서 Open API 호출이 거부되었습니다 (403, %s).\n"
        "[Toss] 현재 공인 IP: %s\n"
        "[Toss] 토스증권 WTS > 설정 > Open API > 허용 IP 관리에서 이 IP를 등록하세요.\n"
        "[Toss] 이후 이 프로세스에서는 yfinance로 강등합니다.",
        context,
        public_ip,
    )


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _detect_market(stock_code: str) -> Optional[str]:
    """Return 'kr'/'us' for symbols TossFetcher can serve, else None."""
    upper = (stock_code or "").strip().upper()
    if is_suffix_market_symbol(upper, "kr"):
        return "kr"
    if is_us_stock_code(upper):
        return "us"
    return None


def _to_toss_symbol(stock_code: str) -> Optional[str]:
    """Convert a repository stock code to Toss's symbol representation.

    KR: '005930.KS' / '005930.KQ' -> '005930' (bare 6-digit code).
    US: 'AAPL' -> 'AAPL' (ticker as-is).
    Anything else (CN/HK/JP/TW/indices) -> None.
    """
    upper = (stock_code or "").strip().upper()
    parts = split_suffix_symbol(upper)
    if parts is not None and is_suffix_market_symbol(upper, "kr"):
        base, _suffix = parts
        return base
    if is_us_stock_code(upper):
        return upper
    return None


def _parse_candle_date(ts: Optional[str]) -> Optional[date]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).date()
    except ValueError:
        return None


class TossFetcher(BaseFetcher):
    """Toss Invest OpenAPI data source (KR realtime-primary, KR daily fallback behind
    yfinance due to NXT-consolidated pricing, US last-resort daily)."""

    name = "TossFetcher"
    # Lowest priority in the current scheme: explicit routing (KR realtime/daily
    # ordering, US daily source_order) decides real ordering wherever Toss
    # participates; this only matters for the un-routed generic fallback.
    priority = 6

    def __init__(self):
        client_id, client_secret = self._load_credentials()
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        if not self._is_configured():
            logger.debug("[Toss] TOSS_CLIENT_ID/TOSS_CLIENT_SECRET not configured, fetcher disabled")

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------

    @staticmethod
    def _load_credentials(config: Any = None) -> Tuple[Optional[str], Optional[str]]:
        if config is None:
            try:
                from src.config import get_config
                config = get_config()
            except Exception:
                config = None
        client_id = _clean(getattr(config, "toss_client_id", None)) if config is not None else None
        client_secret = _clean(getattr(config, "toss_client_secret", None)) if config is not None else None
        client_id = client_id or _clean(os.getenv("TOSS_CLIENT_ID"))
        client_secret = client_secret or _clean(os.getenv("TOSS_CLIENT_SECRET"))
        return client_id, client_secret

    @staticmethod
    def has_configured_credentials(config: Any = None) -> bool:
        """Return True when runtime config/env can attempt Toss auth."""
        client_id, client_secret = TossFetcher._load_credentials(config)
        return bool(client_id and client_secret)

    def _is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def is_available_for_request(self, capability: str = "") -> bool:
        """Report request-time availability, including the process-wide 403 guard."""
        if not self._is_configured():
            return False
        if is_toss_forbidden():
            return False
        return True

    # ------------------------------------------------------------------
    # OAuth2 token management
    # ------------------------------------------------------------------

    def _fetch_token(self) -> Tuple[str, int]:
        try:
            resp = requests.post(
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
        except requests.RequestException as e:
            raise DataFetchError(f"[Toss] token request failed: {e}") from e

        if resp.status_code == 403:
            _mark_forbidden_and_warn("POST /oauth2/token status=403")
            raise DataFetchError("[Toss] 403 Forbidden issuing token (IP not allow-listed)")

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise DataFetchError(f"[Toss] token issuance failed ({resp.status_code}): {e}") from e

        try:
            data = resp.json()
        except ValueError as e:
            raise DataFetchError(f"[Toss] token response is not valid JSON: {e}") from e

        access_token = data.get("access_token")
        if not access_token:
            raise DataFetchError(f"[Toss] token response missing access_token: {data}")

        try:
            expires_in = int(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        return access_token, expires_in

    def _get_access_token(self, force_refresh: bool = False) -> str:
        with self._token_lock:
            now = time.time()
            if not force_refresh and self._access_token and now < self._token_expires_at:
                return self._access_token
            access_token, expires_in = self._fetch_token()
            self._access_token = access_token
            self._token_expires_at = now + max(0, expires_in - _TOKEN_EXPIRY_MARGIN_SECONDS)
            return access_token

    # ------------------------------------------------------------------
    # HTTP request helper: auth header, 401 refresh-once, 429 backoff, 403 visibility
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_retry_after(resp: requests.Response) -> Optional[float]:
        raw = resp.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def _request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        account_seq: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """GET helper shared by every Toss endpoint.

        ``account_seq``, when given, is sent as the ``X-Tossinvest-Account`` header
        required by user-context (account/asset/order-history) endpoints — the
        market-data endpoints (prices/candles/stocks) never pass it.
        """
        if not self._is_configured():
            raise DataFetchError("[Toss] TOSS_CLIENT_ID/TOSS_CLIENT_SECRET not configured")
        if is_toss_forbidden():
            raise DataFetchError("[Toss] previously blocked by IP allow-list (403); skipped for this process")

        retried_auth = False
        attempt = 0
        while True:
            token = self._get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            if account_seq is not None:
                headers[_ACCOUNT_HEADER] = str(account_seq)
            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=15,
                )
            except requests.RequestException as e:
                raise DataFetchError(f"[Toss] HTTP request failed: {e}") from e

            if resp.status_code == 403:
                _mark_forbidden_and_warn(f"GET {url} status=403")
                raise DataFetchError(f"[Toss] 403 Forbidden (IP not allow-listed): {url}")

            if resp.status_code == 401 and not retried_auth:
                retried_auth = True
                logger.debug(f"[Toss] 401 received for {url}, refreshing token once and retrying")
                self._get_access_token(force_refresh=True)
                continue

            if resp.status_code == 429:
                attempt += 1
                if attempt > _RETRY_LIMIT:
                    raise DataFetchError(f"[Toss] rate limited after {_RETRY_LIMIT} retries: {url}")
                retry_after = self._parse_retry_after(resp)
                backoff = retry_after if retry_after is not None else min(2 ** attempt, 8)
                backoff += random.uniform(0, 0.5)
                logger.info(
                    f"[Toss] 429 rate limited on {url}, backing off {backoff:.1f}s "
                    f"(attempt {attempt}/{_RETRY_LIMIT})"
                )
                time.sleep(backoff)
                continue

            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                raise DataFetchError(f"[Toss] HTTP {resp.status_code} for {url}: {e}") from e

            try:
                return resp.json()
            except ValueError as e:
                raise DataFetchError(f"[Toss] invalid JSON response from {url}: {e}") from e

    # ------------------------------------------------------------------
    # get_realtime_quote — GET /api/v1/prices
    # ------------------------------------------------------------------

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        if not self._is_configured() or is_toss_forbidden():
            return None

        market = _detect_market(stock_code)
        symbol = _to_toss_symbol(stock_code)
        if market is None or symbol is None:
            return None

        try:
            payload = self._request(_PRICES_URL, params={"symbols": symbol})
        except DataFetchError as e:
            logger.info(f"[Toss] {symbol} realtime quote failed: {e}")
            return None
        except Exception as e:
            logger.warning(f"[Toss] {symbol} realtime quote failed unexpectedly: {e}")
            return None

        results = payload.get("result") or []
        row = next((r for r in results if str(r.get("symbol", "")).upper() == symbol.upper()), None)
        if row is None and results:
            row = results[0]
        if row is None:
            return None

        price = safe_float(row.get("lastPrice"))
        if price is None or price <= 0:
            return None

        currency = (row.get("currency") or "").strip().upper() or ("KRW" if market == "kr" else "USD")

        # /api/v1/prices only returns lastPrice/symbol/currency/timestamp (confirmed
        # against the OpenAPI spec) — no change/volume/OHLC. Supplement with a small
        # recent-candles call so downstream consumers get change/OHLC/volume when
        # possible; fail open to a price-only "partial" quote if that call fails.
        open_price, high, low, volume, pre_close = self._supplement_from_candles(
            symbol, price_timestamp=row.get("timestamp")
        )

        change_amount: Optional[float] = None
        change_pct: Optional[float] = None
        amplitude: Optional[float] = None
        if pre_close is not None:
            change_amount = round(price - pre_close, 4)
            if pre_close != 0:
                change_pct = round((price - pre_close) / pre_close * 100, 2)
        if high is not None and low is not None and pre_close:
            amplitude = round((high - low) / pre_close * 100, 2)

        missing_fields = [
            name
            for name, val in (
                ("change_pct", change_pct),
                ("change_amount", change_amount),
                ("volume", volume),
                ("open_price", open_price),
                ("high", high),
                ("low", low),
                ("pre_close", pre_close),
            )
            if val is None
        ]
        data_quality = "ok" if not missing_fields else "partial"

        return UnifiedRealtimeQuote(
            code=stock_code,
            source=RealtimeSource.TOSS,
            market=market,
            currency=currency,
            data_quality=data_quality,
            missing_fields=missing_fields or None,
            price=price,
            change_amount=change_amount,
            change_pct=change_pct,
            volume=volume,
            amplitude=amplitude,
            open_price=open_price,
            high=high,
            low=low,
            pre_close=pre_close,
            provider_timestamp=row.get("timestamp"),
        )

    def _supplement_from_candles(
        self, symbol: str, price_timestamp: Optional[str]
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[int], Optional[float]]:
        """Fetch a few recent daily candles to fill in OHLC/volume/pre_close for a quote.

        Returns (open_price, high, low, volume, pre_close). Fails open (all ``None``)
        on any error so the caller always has a usable price-only quote — this is a
        best-effort enrichment, not a hard dependency of ``get_realtime_quote``.

        ``/api/v1/candles`` returns candles newest-first. Two cases, compared by date
        in the response's own timezone offset:
        - latest candle's date == quote timestamp's date: today's bar is already
          published — use it for open/high/low/volume, and the prior candle's close
          as ``pre_close``.
        - latest candle's date < quote timestamp's date: today's bar hasn't been
          published yet — the latest candle's close becomes ``pre_close``; OHLC/volume
          stay ``None`` (there is no "today" bar to report them from).
        """
        try:
            payload = self._request(
                _CANDLES_URL,
                params={"symbol": symbol, "interval": "1d", "count": 3, "adjusted": "true"},
            )
        except Exception as e:
            logger.info(f"[Toss] {symbol} candle supplement failed, returning price-only quote: {e}")
            return None, None, None, None, None

        candles = ((payload.get("result") or {}).get("candles")) or []
        if not candles:
            return None, None, None, None, None

        price_date = _parse_candle_date(price_timestamp)
        latest = candles[0]
        latest_date = _parse_candle_date(latest.get("timestamp"))
        if price_date is None or latest_date is None:
            return None, None, None, None, None

        if latest_date == price_date:
            open_price = safe_float(latest.get("openPrice"))
            high = safe_float(latest.get("highPrice"))
            low = safe_float(latest.get("lowPrice"))
            volume = safe_int(latest.get("volume"))
            pre_close = safe_float(candles[1].get("closePrice")) if len(candles) > 1 else None
            return open_price, high, low, volume, pre_close

        if latest_date < price_date:
            pre_close = safe_float(latest.get("closePrice"))
            return None, None, None, None, pre_close

        # latest_date > price_date: unexpected ordering, don't guess
        return None, None, None, None, None

    # ------------------------------------------------------------------
    # get_daily_data — GET /api/v1/candles (BaseFetcher abstract methods)
    # ------------------------------------------------------------------

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        if not self._is_configured():
            raise DataFetchError("[Toss] TOSS_CLIENT_ID/TOSS_CLIENT_SECRET not configured")
        if is_toss_forbidden():
            raise DataFetchError("[Toss] previously blocked by IP allow-list (403); skipped for this process")

        symbol = _to_toss_symbol(stock_code)
        if symbol is None:
            raise DataFetchError(f"[Toss] {stock_code} is not a supported KR/US symbol")

        try:
            start_d = datetime.strptime(start_date, "%Y-%m-%d").date()
            end_d = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as e:
            raise DataFetchError(f"[Toss] invalid date range {start_date}~{end_date}: {e}") from e

        all_rows: List[Dict[str, Any]] = []
        before: Optional[str] = None
        for _page in range(_MAX_CANDLE_PAGES):
            params: Dict[str, Any] = {
                "symbol": symbol,
                "interval": "1d",
                "count": _MAX_CANDLES_PER_CALL,
                "adjusted": "true",
            }
            if before:
                params["before"] = before

            payload = self._request(_CANDLES_URL, params=params)
            result = payload.get("result") or {}
            candles = result.get("candles") or []
            if not candles:
                break

            all_rows.extend(candles)

            oldest_date = _parse_candle_date(candles[-1].get("timestamp"))
            next_before = result.get("nextBefore")
            if not next_before or (oldest_date is not None and oldest_date <= start_d):
                break
            before = next_before

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        dates = df["timestamp"].apply(_parse_candle_date)
        mask = dates.notna() & (dates >= start_d) & (dates <= end_d)
        df = df.loc[mask].reset_index(drop=True)
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        df = df.copy()
        df["date"] = df["timestamp"].apply(_parse_candle_date)
        df = df.rename(columns={
            "openPrice": "open",
            "highPrice": "high",
            "lowPrice": "low",
            "closePrice": "close",
        })
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 'before' pagination is inclusive of its boundary timestamp, so the last
        # row of one page can reappear as the first row of the next — de-dup by date.
        df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)

        df["pct_chg"] = df["close"].pct_change() * 100
        df["pct_chg"] = df["pct_chg"].fillna(0).round(2)
        df["amount"] = df["volume"] * df["close"]
        df["code"] = stock_code

        keep = ["code"] + STANDARD_COLUMNS
        return df[[c for c in keep if c in df.columns]]

    # ------------------------------------------------------------------
    # get_stocks_master — GET /api/v1/stocks (offline build-tool support)
    # ------------------------------------------------------------------

    def get_stocks_master(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch stock master info (name/status/market/...) via ``GET /api/v1/stocks``.

        Used by the offline ``scripts/fetch_kr_stock_list.py`` build tool to verify
        and enrich the FDR-sourced KR listing (Korean name, delisting status) — this
        is not part of the runtime fetcher capability set (no other caller invokes
        it), so it never affects analysis request paths. Batches up to
        ``_MAX_STOCKS_PER_CALL`` symbols/call (Toss ``STOCK`` rate-limit group is
        5 TPS), sleeping ``_STOCKS_BATCH_SLEEP_SECONDS`` between calls.

        Fails open per-batch: a failed batch is logged and skipped rather than
        raised, so callers that already have data for those symbols (e.g. FDR) keep
        using it instead of losing the whole run to one bad batch.

        Returns a dict keyed by uppercased symbol -> raw ``StockInfo`` payload.
        """
        if not self._is_configured() or is_toss_forbidden():
            return {}

        cleaned = [s.strip().upper() for s in symbols if s and s.strip()]
        out: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(cleaned), _MAX_STOCKS_PER_CALL):
            batch = cleaned[i : i + _MAX_STOCKS_PER_CALL]
            if i > 0:
                time.sleep(_STOCKS_BATCH_SLEEP_SECONDS)
            try:
                payload = self._request(_STOCKS_URL, params={"symbols": ",".join(batch)})
            except Exception as e:
                logger.warning(
                    f"[Toss] stocks master batch failed ({batch[0]}..{batch[-1]}, n={len(batch)}): {e}"
                )
                continue
            for item in payload.get("result") or []:
                symbol = str(item.get("symbol", "")).strip().upper()
                if symbol:
                    out[symbol] = item
        return out

    # ------------------------------------------------------------------
    # Portfolio broker-link support (Phase 2) — read-only account/asset/order
    # endpoints only. This fetcher never calls order create/modify/cancel APIs.
    # ------------------------------------------------------------------

    def get_accounts(self) -> List[Dict[str, Any]]:
        """List the caller's brokerage accounts via ``GET /api/v1/accounts``
        (``ACCOUNT`` rate-limit group, 1 TPS).

        Unlike the passive quote-fetching methods above, this raises
        ``DataFetchError`` on any failure (not configured, 403 IP not allow-listed,
        network, non-2xx) instead of failing open to an empty result — an explicit
        broker-link/sync action must surface the real reason rather than look like
        "the user has zero accounts".
        """
        payload = self._request(_ACCOUNTS_URL)
        return list(payload.get("result") or [])

    def get_holdings(self, account_seq: Any) -> Dict[str, Any]:
        """Fetch current holdings for one account via ``GET /api/v1/holdings``
        (``ASSET`` rate-limit group, 5 TPS). Requires the account-context header.

        Raises ``DataFetchError`` on failure (see ``get_accounts``).
        """
        payload = self._request(_HOLDINGS_URL, account_seq=account_seq)
        return payload.get("result") or {}

    def get_closed_orders(
        self,
        account_seq: Any,
        *,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch every ``CLOSED`` order for one account via ``GET /api/v1/orders``
        (``ORDER_HISTORY`` rate-limit group, 5 TPS), paginating with the cursor
        until exhausted.

        ``CLOSED`` is a terminal order status, not "filled" — a fully-canceled
        order with zero fills can still be ``CLOSED``. This method returns every
        such order as-is; filtering to orders with an actual fill
        (``execution.filledQuantity > 0``) is the caller's responsibility
        (``portfolio_broker_sync_service``), matching the design spec's "reflect
        every CLOSED order with a nonzero fill, including partial fills followed
        by cancel/replace" contract.

        Envelope shape is the one confirmed against the live API:
        ``{"result": {"orders": [...], "nextCursor": str | None, "hasNext": bool}}``.
        This is validated *strictly* (design spec §3 "envelope 엄격 검증"): a
        missing ``result``/``orders``/``hasNext``/``nextCursor`` key, a wrong
        type for any of them, ``hasNext=true`` with an empty ``nextCursor``, or
        exhausting the ``_ORDERS_MAX_PAGES`` safety cap while ``hasNext`` is
        still ``true`` all raise ``DataFetchError`` instead of returning a
        partial/empty result — a schema surprise must never look like "the sync
        found zero new orders" to the caller, which would silently look like a
        clean, up-to-date sync and advance the cursor past real fills. There is
        no defensive alternate-key guessing (``items``/``data``/``cursor``/
        ``next_cursor``): a real envelope shift must fail loudly, not degrade
        silently into a wrong pagination decision.
        """
        orders: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        params_base: Dict[str, Any] = {
            "status": _ORDERS_STATUS_CLOSED,
            "limit": _ORDERS_PAGE_LIMIT,
        }
        if from_date is not None:
            params_base["from"] = from_date.isoformat()
        if to_date is not None:
            params_base["to"] = to_date.isoformat()

        for page in range(_ORDERS_MAX_PAGES):
            params = dict(params_base)
            if cursor:
                params["cursor"] = cursor
            if page > 0:
                time.sleep(_ORDERS_PAGE_SLEEP_SECONDS)

            payload = self._request(_ORDERS_URL, params=params, account_seq=account_seq)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise DataFetchError(
                    f"[Toss] orders envelope missing object 'result' for account_seq={account_seq}: {payload!r}"
                )

            page_orders = result.get("orders")
            if not isinstance(page_orders, list):
                raise DataFetchError(
                    f"[Toss] orders envelope missing/invalid 'result.orders' list for "
                    f"account_seq={account_seq}: {result!r}"
                )

            if "hasNext" not in result or not isinstance(result.get("hasNext"), bool):
                raise DataFetchError(
                    f"[Toss] orders envelope missing/invalid 'result.hasNext' bool for "
                    f"account_seq={account_seq}: {result!r}"
                )
            has_next = result["hasNext"]

            if "nextCursor" not in result:
                raise DataFetchError(
                    f"[Toss] orders envelope missing 'result.nextCursor' key for "
                    f"account_seq={account_seq}: {result!r}"
                )
            next_cursor = result["nextCursor"]
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise DataFetchError(
                    f"[Toss] orders envelope 'result.nextCursor' must be str or null for "
                    f"account_seq={account_seq}: {next_cursor!r}"
                )

            if has_next and not next_cursor:
                raise DataFetchError(
                    f"[Toss] orders envelope hasNext=true but nextCursor is empty for "
                    f"account_seq={account_seq}"
                )

            orders.extend(page_orders)

            if not has_next:
                break
            cursor = next_cursor
        else:
            raise DataFetchError(
                f"[Toss] get_closed_orders hit the {_ORDERS_MAX_PAGES}-page safety cap "
                f"for account_seq={account_seq} with hasNext still true; refusing to "
                f"return a partial result"
            )

        return orders
