#!/usr/bin/env python3
import argparse
import csv
import hashlib
import html as html_lib
import json
import logging
import os
import re
import subprocess
import shutil
import time
from datetime import timedelta
from datetime import datetime, timezone
from datetime import date as date_cls
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import quote, unquote, urlparse
import threading

import feedparser
import requests

TRADE_HALTS_RSS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NASDAQ_TRADE_HALTS_PAGE = "https://www.nasdaqtrader.com/Trader.aspx?id=TradeHalts"
NYSE_TRADE_HALTS_CSV = "https://www.nyse.com/api/trade-halts/current/download"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
FMP_QUOTE_URL = "https://financialmodelingprep.com/api/v3/quote/{ticker}?apikey={apikey}"

STATE_PATH = Path.home() / ".tradehaltalerts_state.json"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "halt_alerts.log"

POLL_SECONDS = 60
TEST_DELAY_FIRST = os.environ.get("HALT_ALERTS_TEST_DELAY_FIRST")
TEST_DELAY_SECOND = os.environ.get("HALT_ALERTS_TEST_DELAY_SECOND")
TEST_MODE_FLAG = os.environ.get("HALT_ALERTS_TEST_MODE")
TEST_MODE = bool(TEST_MODE_FLAG or TEST_DELAY_FIRST or TEST_DELAY_SECOND)
USER_AGENT = "TradeHaltAlerts/1.0"
X_API_BEARER_TOKEN = os.environ.get("X_API_BEARER_TOKEN")
X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
DETAILS_PORT = int(os.environ.get("HALT_ALERTS_DETAILS_PORT", "8787"))
DETAILS_HOST = "127.0.0.1"
DETAILS_AVAILABLE = False
TERMINAL_NOTIFIER_SENDER = os.environ.get("TERMINAL_NOTIFIER_SENDER")
TERMINAL_NOTIFIER_ACTIVATE = os.environ.get("TERMINAL_NOTIFIER_ACTIVATE")
OPEN_DETAILS = os.environ.get("HALT_ALERTS_OPEN_DETAILS", "").lower() == "auto"
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT")
INTRADAY_LOOKBACK_MINUTES = int(os.environ.get("HALT_ALERTS_INTRADAY_LOOKBACK_MINUTES", "5"))
AI_CATALYST_ENABLED = os.environ.get("HALT_ALERTS_AI_CATALYST", "").lower() == "1"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
FINRA_SHORT_INTEREST_DATE = os.environ.get("FINRA_SHORT_INTEREST_DATE")

ALPHAVANTAGE_QUOTE_URL = "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={ticker}&apikey={apikey}"
ALPHAVANTAGE_INTRADAY_URL = (
    "https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY&symbol={ticker}"
    "&interval=1min&outputsize=compact&apikey={apikey}"
)
TWELVEDATA_INTRADAY_URL = (
    "https://api.twelvedata.com/time_series?symbol={ticker}&interval=1min&outputsize=30&apikey={apikey}"
)
TWELVEDATA_QUOTE_URL = "https://api.twelvedata.com/quote?symbol={ticker}&apikey={apikey}"
SEC_TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FINRA_SHORT_INTEREST_URL = "https://api.finra.org/data/group/otcMarket/name/ConsolidatedShortInterest"
FINRA_PARTITIONS_URL = "https://api.finra.org/partitions/group/otcMarket/name/ConsolidatedShortInterest"
FINRA_SCHEDULE_URL = "https://www.finra.org/filing-reporting/regulatory-filing-systems/short-interest"

_SEC_TICKER_CIK_CACHE = {"data": {}, "timestamp": 0.0}
_SEC_COMPANY_FACTS_CACHE = {"data": {}, "timestamp": 0.0}
_SEC_UA_WARNED = False
_AV_INTRADAY_CACHE = {}
_TD_INTRADAY_CACHE = {}
_FINRA_SI_CACHE = {}
_FINRA_CALENDAR_CACHE = {"date": None, "dates": [], "timestamp": 0.0}
_FINRA_PARTITION_CACHE = {"dates": [], "timestamp": 0.0}

LULD_CODES = {"LUDP", "LUDS", "M"}

HALT_CODE_DESCRIPTIONS = {
    "T1": "Halt - News Pending. Trading is halted pending the release of material news.",
    "T2": "Halt - News Released. The news has begun the dissemination process through a Regulation FD compliant method.",
    "T3": "News and Resumption Times. The news has been fully disseminated through a Regulation FD compliant method or NASDAQ has determined conditions leading to a halt are no longer present. Two times will be displayed: time for quotations, then time for trading. Times are in HH:MM:SS.",
    "T5": "Single Stock Trading Pause in Effect. Trading has been paused by NASDAQ due to a 10 percent or more price move in the security in a five minute period.",
    "T6": "Halt - Extraordinary Market Activity. Trading is halted when extraordinary market activity is occurring and is likely to have a material effect on the market for that security.",
    "T7": "Single Stock Trading Pause/Quotation-Only Period. Quotations have resumed for affected security, but trading remains paused.",
    "T8": "Halt - Exchange-Traded-Fund (ETF). Trading is halted in an ETF due to factors such as trading ceasing in underlying securities or other unusual conditions.",
    "T12": "Halt - Additional Information Requested by NASDAQ. Trading is halted pending receipt of additional information requested by NASDAQ.",
    "H4": "Halt - Non-compliance. Trading is halted due to the company's non-compliance with NASDAQ listing requirements.",
    "H9": "Halt - Not Current. Trading is halted because the company is not current in its required filings.",
    "H10": "Halt - SEC Trading Suspension. The Securities and Exchange Commission has suspended trading in this stock.",
    "H11": "Halt - Regulatory Concern. Trading is halted in conjunction with another exchange or market for regulatory reasons.",
    "O1": "Operations Halt, Contact Market Operations.",
    "IPO1": "IPO Issue not yet Trading.",
    "IPOQ": "IPO security released for quotation.",
    "IPOE": "IPO security - positioning window extension.",
    "M1": "Corporate Action.",
    "M2": "Quotation Not Available.",
    "LUDP": "Volatility Trading Pause.",
    "LUDS": "Volatility Trading Pause - Straddle Condition.",
    "MWC1": "Market Wide Circuit Breaker Halt - Level 1.",
    "MWC2": "Market Wide Circuit Breaker Halt - Level 2.",
    "MWC3": "Market Wide Circuit Breaker Halt - Level 3.",
    "MWC0": "Market Wide Circuit Breaker Halt - Carry over from previous day.",
    "MWCQ": "Market Wide Circuit Breaker Resumption.",
    "R4": "Qualifications Issues Reviewed/Resolved. Quotations and trading to resume.",
    "R9": "Filing Requirements Satisfied/Resolved. Quotations and trading to resume.",
    "C3": "Issuer News Not Forthcoming. Quotations and trading to resume.",
    "C4": "Qualifications Halt ended, maintenance requirements met. Resume.",
    "C9": "Qualifications Halt Concluded. Filings Met. Quotes and trades to resume.",
    "C11": "Trade Halt Concluded By Other Regulatory Authority. Quotes and trades resume.",
    "R1": "New Issue Available.",
    "R2": "Issue Available.",
    "M": "Volatility Trading Pause. Trading has been paused in an Exchange-Listed issue (Market Category Code = C).",
    "D": "Security deletion from NASDAQ or CQS.",
}


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


def sec_headers() -> Optional[dict]:
    global _SEC_UA_WARNED
    if not SEC_USER_AGENT:
        if not _SEC_UA_WARNED:
            logging.warning("SEC_USER_AGENT not set. SEC endpoints will be skipped.")
            _SEC_UA_WARNED = True
        return None
    return {"User-Agent": SEC_USER_AGENT}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "seen_ids": [],
            "pending_resumes": [],
            "halt_counts": {},
            "last_poll": 0,
            "recent_alerts": [],
            "paused": False,
            "catalyst_cache": {},
        }
    try:
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {
                "seen_ids": [],
                "pending_resumes": [],
                "halt_counts": {},
                "last_poll": 0,
                "recent_alerts": [],
                "paused": False,
                "catalyst_cache": {},
            }
        data.setdefault("seen_ids", [])
        data.setdefault("pending_resumes", [])
        data.setdefault("halt_counts", {})
        data.setdefault("last_poll", 0)
        data.setdefault("recent_alerts", [])
        data.setdefault("paused", False)
        data.setdefault("catalyst_cache", {})
        if not isinstance(data["seen_ids"], list):
            data["seen_ids"] = []
        if not isinstance(data["pending_resumes"], list):
            data["pending_resumes"] = []
        if not isinstance(data["halt_counts"], dict):
            data["halt_counts"] = {}
        if not isinstance(data["recent_alerts"], list):
            data["recent_alerts"] = []
        if not isinstance(data["paused"], bool):
            data["paused"] = False
        if not isinstance(data["catalyst_cache"], dict):
            data["catalyst_cache"] = {}
        return data
    except Exception as exc:
        logging.warning("Failed to load state, starting fresh: %s", exc)
        return {
            "seen_ids": [],
            "pending_resumes": [],
            "halt_counts": {},
            "last_poll": 0,
            "recent_alerts": [],
            "paused": False,
            "catalyst_cache": {},
        }


def save_state(state: dict) -> None:
    try:
        with STATE_PATH.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
    except Exception as exc:
        logging.warning("Failed to save state: %s", exc)


def record_alert(state: dict, alert: dict) -> None:
    alerts = state.setdefault("recent_alerts", [])
    event_id = alert.get("event_id")
    if event_id:
        alerts[:] = [a for a in alerts if a.get("event_id") != event_id]
    alerts.append(alert)
    if len(alerts) > 200:
        del alerts[:-200]


def request_with_retries(url: str, timeout: int = 20, max_attempts: int = 3) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, timeout=timeout, headers=headers)
            response.raise_for_status()
            return response.content
        except Exception as exc:
            last_error = exc
            logging.warning("Fetch failed (%s/%s) for %s: %s", attempt, max_attempts, url, exc)
            time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def fetch_rss(url: str) -> feedparser.FeedParserDict:
    content = request_with_retries(url, timeout=20, max_attempts=3)
    return feedparser.parse(content)


def fetch_alphavantage_quote(ticker: str) -> dict:
    if not ALPHAVANTAGE_API_KEY:
        return {}
    try:
        url = ALPHAVANTAGE_QUOTE_URL.format(ticker=ticker, apikey=ALPHAVANTAGE_API_KEY)
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        quote = data.get("Global Quote", {})
        price_str = quote.get("05. price") or quote.get("05. price ")
        prev_close_str = quote.get("08. previous close") or quote.get("08. previous close ")
        result = {}
        if price_str:
            result["price"] = float(price_str)
        if prev_close_str:
            result["prev_close"] = float(prev_close_str)
        return result
    except Exception as exc:
        logging.warning("Alpha Vantage price failed for %s: %s", ticker, exc)
        return {}


def fetch_twelvedata_quote(ticker: str) -> dict:
    if not TWELVEDATA_API_KEY:
        return {}
    try:
        url = TWELVEDATA_QUOTE_URL.format(ticker=ticker, apikey=TWELVEDATA_API_KEY)
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "error":
            raise ValueError(data.get("message") or "twelvedata error")
        result = {}
        close_str = data.get("close")
        prev_close_str = data.get("previous_close")
        if close_str:
            result["price"] = float(close_str)
        if prev_close_str:
            result["prev_close"] = float(prev_close_str)
        return result
    except Exception as exc:
        logging.warning("Twelve Data quote failed for %s: %s", ticker, exc)
        return {}


def fetch_alphavantage_intraday(ticker: str) -> list:
    if not ALPHAVANTAGE_API_KEY:
        return []
    now = time.time()
    cached = _AV_INTRADAY_CACHE.get(ticker)
    if cached and now - cached.get("timestamp", 0) < 55:
        return cached.get("series", [])
    try:
        url = ALPHAVANTAGE_INTRADAY_URL.format(ticker=ticker, apikey=ALPHAVANTAGE_API_KEY)
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        series = data.get("Time Series (1min)", {})
        points = []
        for ts, values in series.items():
            close_str = values.get("4. close")
            if not close_str:
                continue
            try:
                dt = datetime.fromisoformat(ts)
                close = float(close_str)
            except ValueError:
                continue
            points.append((dt, close))
        points.sort(key=lambda x: x[0], reverse=True)
        _AV_INTRADAY_CACHE[ticker] = {"timestamp": now, "series": points}
        return points
    except Exception as exc:
        logging.warning("Alpha Vantage intraday failed for %s: %s", ticker, exc)
        return []


def fetch_twelvedata_intraday(ticker: str) -> list:
    if not TWELVEDATA_API_KEY:
        return []
    now = time.time()
    cached = _TD_INTRADAY_CACHE.get(ticker)
    if cached and now - cached.get("timestamp", 0) < 55:
        return cached.get("series", [])
    try:
        url = TWELVEDATA_INTRADAY_URL.format(ticker=ticker, apikey=TWELVEDATA_API_KEY)
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "error":
            logging.warning("Twelve Data intraday error for %s: %s", ticker, data.get("message"))
            return []
        values = data.get("values", [])
        points = []
        for item in values:
            dt_str = item.get("datetime")
            close_str = item.get("close")
            if not dt_str or not close_str:
                continue
            try:
                dt = datetime.fromisoformat(dt_str)
                close = float(close_str)
            except ValueError:
                continue
            points.append((dt, close))
        points.sort(key=lambda x: x[0], reverse=True)
        _TD_INTRADAY_CACHE[ticker] = {"timestamp": now, "series": points}
        return points
    except Exception as exc:
        logging.warning("Twelve Data intraday failed for %s: %s", ticker, exc)
        return []


def fetch_sec_ticker_cik_map() -> dict:
    now = time.time()
    cached = _SEC_TICKER_CIK_CACHE.get("data", {})
    if cached and now - _SEC_TICKER_CIK_CACHE.get("timestamp", 0) < 24 * 3600:
        return cached
    try:
        headers = sec_headers()
        if not headers:
            return cached
        response = requests.get(SEC_TICKER_CIK_URL, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        mapping = {}
        for entry in data.values():
            ticker = str(entry.get("ticker", "")).upper()
            cik = str(entry.get("cik_str", "")).zfill(10)
            if ticker and cik:
                mapping[ticker] = cik
        _SEC_TICKER_CIK_CACHE["data"] = mapping
        _SEC_TICKER_CIK_CACHE["timestamp"] = now
        return mapping
    except Exception as exc:
        logging.warning("SEC ticker map fetch failed: %s", exc)
        return cached


def select_latest_fact(items: list) -> Optional[float]:
    best_val = None
    best_date = None
    for item in items:
        end = item.get("end") or item.get("instant")
        val = item.get("val")
        if end is None or val is None:
            continue
        try:
            end_dt = datetime.fromisoformat(end)
        except ValueError:
            continue
        if best_date is None or end_dt > best_date:
            best_date = end_dt
            best_val = val
    return best_val


def fetch_sec_company_facts(ticker: str) -> Optional[dict]:
    try:
        now = time.time()
        cached = _SEC_COMPANY_FACTS_CACHE.get("data", {})
        cached_entry = cached.get(ticker.upper())
        if cached_entry and now - _SEC_COMPANY_FACTS_CACHE.get("timestamp", 0) < 24 * 3600:
            return cached_entry
        cik_map = fetch_sec_ticker_cik_map()
        cik = cik_map.get(ticker.upper())
        if not cik:
            return None
        url = SEC_COMPANY_FACTS_URL.format(cik=cik)
        headers = sec_headers()
        if not headers:
            return None
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        cached[ticker.upper()] = data
        _SEC_COMPANY_FACTS_CACHE["data"] = cached
        _SEC_COMPANY_FACTS_CACHE["timestamp"] = now
        return data
    except Exception as exc:
        logging.warning("SEC company facts failed for %s: %s", ticker, exc)
        return None


def fetch_sec_shares_outstanding(ticker: str) -> Optional[float]:
    try:
        data = fetch_sec_company_facts(ticker)
        if not data:
            return None
        facts = data.get("facts", {})
        candidates = [
            ("us-gaap", "CommonStockSharesOutstanding"),
            ("dei", "EntityCommonStockSharesOutstanding"),
        ]
        for namespace, tag in candidates:
            tag_data = facts.get(namespace, {}).get(tag, {})
            units = tag_data.get("units", {})
            for unit_values in units.values():
                val = select_latest_fact(unit_values)
                if val is not None:
                    return float(val)
        return None
    except Exception as exc:
        logging.warning("SEC shares outstanding failed for %s: %s", ticker, exc)
        return None


def fetch_sec_public_float(ticker: str) -> Optional[float]:
    try:
        data = fetch_sec_company_facts(ticker)
        if not data:
            return None
        facts = data.get("facts", {})
        candidates = [
            ("us-gaap", "PublicFloat"),
            ("dei", "EntityPublicFloat"),
        ]
        for namespace, tag in candidates:
            tag_data = facts.get(namespace, {}).get(tag, {})
            units = tag_data.get("units", {})
            for unit_values in units.values():
                val = select_latest_fact(unit_values)
                if val is not None:
                    return float(val)
        return None
    except Exception as exc:
        logging.warning("SEC public float failed for %s: %s", ticker, exc)
        return None


def fetch_finra_short_interest(ticker: str) -> dict:
    now = time.time()
    cached = _FINRA_SI_CACHE.get(ticker)
    if cached and now - cached.get("timestamp", 0) < 12 * 3600:
        return cached.get("data", {})
    try:
        dates = fetch_finra_calendar_dates()
        if not dates:
            return {}
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT}
        for settlement_date in dates[:5]:
            compare_filters = [
                {"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": ticker.upper()}
            ]
            compare_filters.append(
                {"compareType": "EQUAL", "fieldName": "settlementDate", "fieldValue": settlement_date}
            )
            payload = {"compareFilters": compare_filters, "limit": 50}
            response = requests.post(FINRA_SHORT_INTEREST_URL, json=payload, headers=headers, timeout=20)
            response.raise_for_status()
            if response.status_code == 204 or not response.text.strip():
                continue
            try:
                data = response.json()
            except Exception:
                snippet = response.text[:200].replace("\n", " ")
                logging.warning(
                    "FINRA short interest non-JSON response for %s: status=%s content-type=%s length=%s snippet=%s",
                    ticker,
                    response.status_code,
                    response.headers.get("Content-Type"),
                    len(response.text),
                    snippet,
                )
                continue
            if not isinstance(data, list) or not data:
                continue
            # pick latest by settlementDate
            def parse_date(item):
                try:
                    return datetime.fromisoformat(item.get("settlementDate", ""))
                except ValueError:
                    return datetime.min

            latest = max(data, key=parse_date)
            result = {
                "short_interest_shares": latest.get("currentShortPositionQuantity"),
                "short_interest_date": latest.get("settlementDate"),
                "days_to_cover": latest.get("daysToCoverQuantity"),
            }
            _FINRA_SI_CACHE[ticker] = {"timestamp": now, "data": result}
            return result
        return {}
    except Exception as exc:
        logging.warning("FINRA short interest failed for %s: %s", ticker, exc)
        return {}


def get_finra_latest_settlement_date() -> Optional[str]:
    if FINRA_SHORT_INTEREST_DATE:
        return FINRA_SHORT_INTEREST_DATE
    now = time.time()
    cached = _FINRA_CALENDAR_CACHE.get("date")
    if cached and now - _FINRA_CALENDAR_CACHE.get("timestamp", 0) < 24 * 3600:
        return cached
    settlement = fetch_finra_settlement_from_calendar()
    _FINRA_CALENDAR_CACHE["date"] = settlement
    _FINRA_CALENDAR_CACHE["timestamp"] = now
    if settlement:
        return settlement
    cached_partitions = _FINRA_PARTITION_CACHE.get("dates", [])
    if cached_partitions and now - _FINRA_PARTITION_CACHE.get("timestamp", 0) < 24 * 3600:
        return cached_partitions[0] if cached_partitions else None
    try:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        response = requests.get(FINRA_PARTITIONS_URL, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        dates = []
        candidates = []
        if isinstance(data, dict):
            candidates.append(data.get("partitions"))
            candidates.append(data.get("availablePartitions"))
        if isinstance(data, list):
            candidates.append(data)
        for entry in candidates:
            if not entry:
                continue
            if isinstance(entry, list):
                for item in entry:
                    if isinstance(item, dict):
                        parts = item.get("partitions") or item.get("values") or item.get("availablePartitions")
                        if isinstance(parts, list):
                            dates.extend(parts)
                    elif isinstance(item, str):
                        dates.append(item)
            elif isinstance(entry, dict):
                parts = entry.get("partitions") or entry.get("values")
                if isinstance(parts, list):
                    dates.extend(parts)
        dates = [d for d in dates if isinstance(d, str) and re.match(r"20\d{2}-\d{2}-\d{2}", d)]
        dates = sorted(set(dates), reverse=True)
        _FINRA_PARTITION_CACHE["dates"] = dates
        _FINRA_PARTITION_CACHE["timestamp"] = now
        if dates:
            return dates[0]
    except Exception as exc:
        logging.warning("FINRA partition lookup failed: %s", exc)
    logging.warning("FINRA calendar did not return a settlement date")
    return None


def fetch_finra_calendar_dates() -> list[str]:
    if FINRA_SHORT_INTEREST_DATE:
        return [FINRA_SHORT_INTEREST_DATE]
    now = time.time()
    cached_dates = _FINRA_CALENDAR_CACHE.get("dates", [])
    if cached_dates and now - _FINRA_CALENDAR_CACHE.get("timestamp", 0) < 24 * 3600:
        return cached_dates
    latest = fetch_finra_settlement_from_calendar()
    dates = _FINRA_CALENDAR_CACHE.get("dates", [])
    if latest and latest not in dates:
        dates.insert(0, latest)
    return dates


def fetch_finra_settlement_from_calendar() -> Optional[str]:
    try:
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(FINRA_SCHEDULE_URL, headers=headers, timeout=20)
        response.raise_for_status()
        text = response.text
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = clean.replace("&nbsp;", " ")
        # find year sections like "2026 Short Interest Reporting Dates"
        year_matches = list(re.finditer(r"(20\d{2})\s+Short Interest Reporting Dates", clean))
        months = {
            "January": 1,
            "February": 2,
            "March": 3,
            "April": 4,
            "May": 5,
            "June": 6,
            "July": 7,
            "August": 8,
            "September": 9,
            "October": 10,
            "November": 11,
            "December": 12,
        }
        dates = []
        month_pattern = r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})"
        month_short_pattern = (
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+(\d{1,2})\s*,?\s*(20\d{2})"
        )
        month_short_map = {
            "Jan": "January",
            "Feb": "February",
            "Mar": "March",
            "Apr": "April",
            "May": "May",
            "Jun": "June",
            "Jul": "July",
            "Aug": "August",
            "Sep": "September",
            "Sept": "September",
            "Oct": "October",
            "Nov": "November",
            "Dec": "December",
        }
        if year_matches:
            for idx, match in enumerate(year_matches):
                year = int(match.group(1))
                start = match.end()
                end = year_matches[idx + 1].start() if idx + 1 < len(year_matches) else len(text)
                section = clean[start:end]
                for month_name, day_str in re.findall(month_pattern, section):
                    try:
                        dt = date_cls(year, months[month_name], int(day_str))
                    except Exception:
                        continue
                    dates.append(dt)
                for short_month, day_str, year_str in re.findall(month_short_pattern, section):
                    month_name = month_short_map.get(short_month)
                    if not month_name:
                        continue
                    try:
                        dt = date_cls(int(year_str), months[month_name], int(day_str))
                    except Exception:
                        continue
                    dates.append(dt)
        else:
            # fallback: assume current year if no headers
            year = date_cls.today().year
            for month_name, day_str in re.findall(month_pattern, clean):
                try:
                    dt = date_cls(year, months[month_name], int(day_str))
                except Exception:
                    continue
                dates.append(dt)
            for short_month, day_str, year_str in re.findall(month_short_pattern, clean):
                month_name = month_short_map.get(short_month)
                if not month_name:
                    continue
                try:
                    dt = date_cls(int(year_str), months[month_name], int(day_str))
                except Exception:
                    continue
                dates.append(dt)
            for month_str, day_str, year_str in re.findall(r"(\d{1,2})/(\d{1,2})/(20\d{2})", clean):
                try:
                    dt = date_cls(int(year_str), int(month_str), int(day_str))
                except Exception:
                    continue
                dates.append(dt)
        if not dates:
            return None
        today = date_cls.today()
        past_dates = sorted({d for d in dates if d <= today}, reverse=True)
        if not past_dates:
            return None
        iso_dates = [d.isoformat() for d in past_dates]
        _FINRA_CALENDAR_CACHE["dates"] = iso_dates
        latest = past_dates[0]
        return latest.isoformat()
    except Exception as exc:
        logging.warning("FINRA calendar scrape failed: %s", exc)
        return None


def fetch_ai_catalyst(state: dict, event_id: str, payload: dict) -> dict:
    if not AI_CATALYST_ENABLED or not OPENAI_API_KEY:
        return {}
    cache = state.setdefault("catalyst_cache", {})
    cached = cache.get(event_id)
    if isinstance(cached, dict) and cached.get("label"):
        return cached

    system = (
        "You are a concise financial news classifier. "
        "Given headlines and context, label catalyst strength as strong, moderate, weak, or noise. "
        "Return strict JSON with keys: label, confidence, rationale."
    )
    user = json.dumps(payload, ensure_ascii=False)
    body = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_output_tokens": 200,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    try:
        response = requests.post(OPENAI_RESPONSES_URL, headers=headers, json=body, timeout=20)
        response.raise_for_status()
        data = response.json()
        text = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                parts = item.get("content", [])
                for part in parts:
                    if part.get("type") == "output_text":
                        text += part.get("text", "")
        result = json.loads(text) if text else {}
        label = str(result.get("label", "")).strip().lower()
        confidence = float(result.get("confidence", 0))
        rationale = str(result.get("rationale", "")).strip()
        if label not in {"strong", "moderate", "weak", "noise"}:
            return {}
        out = {"label": label, "confidence": confidence, "rationale": rationale}
        cache[event_id] = out
        save_state(state)
        return out
    except Exception as exc:
        logging.warning("AI catalyst failed: %s", exc)
        return {}

def get_first(entry: dict, keys: List[str], default: str = "") -> str:
    for key in keys:
        value = entry.get(key)
        if value:
            return str(value).strip()
    return default


def parse_html_table(html_text: str) -> List[dict]:
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html_text, flags=re.IGNORECASE | re.DOTALL)
    header = []
    parsed_rows = []
    for row in rows:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, flags=re.IGNORECASE | re.DOTALL)
        if not cells:
            continue
        clean_cells = []
        for cell in cells:
            text = re.sub(r"<[^>]+>", "", cell)
            text = html_lib.unescape(text).strip()
            clean_cells.append(text)
        if not header:
            header = clean_cells
            continue
        if header and len(clean_cells) == len(header):
            parsed_rows.append(dict(zip(header, clean_cells)))
    return parsed_rows


def normalize_row(row: dict) -> dict:
    mapping = {
        "Halt Date": "haltdate",
        "Halt Time": "halttime",
        "Issue Symbol": "symbol",
        "Symbol": "symbol",
        "Reason Code": "reasoncode",
        "Reason": "reasoncode",
        "Resume Date": "resumedate",
        "Resume Time": "resumetime",
        "NYSE Resume Time": "resumetime",
        "Name": "name",
        "Exchange": "exchange",
    }
    normalized = {}
    for key, value in row.items():
        if key in mapping:
            normalized[mapping[key]] = value.strip() if isinstance(value, str) else value
    return normalized


def parse_halt_datetime(entry: dict) -> Optional[float]:
    date_raw = get_first(entry, ["haltdate", "halt_date", "date"])
    time_raw = get_first(entry, ["halttime", "halt_time"])
    if not date_raw or not time_raw:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return time.mktime(time.strptime(f"{date_raw} {time_raw}", fmt))
        except ValueError:
            continue
    return None


def compute_halt_direction(reason_code: str, market: dict) -> str:
    code = reason_code.strip().upper()
    if code not in LULD_CODES:
        return ""
    lookback = max(2, min(5, INTRADAY_LOOKBACK_MINUTES))
    ticker = market.get("ticker", "")
    series = fetch_twelvedata_intraday(ticker) if ticker else []
    if not series and ticker:
        series = fetch_alphavantage_intraday(ticker)
    if series:
        latest_dt, latest_close = series[0]
        target_dt = latest_dt - timedelta(minutes=lookback)
        past_close = None
        for dt, close in series:
            if dt <= target_dt:
                past_close = close
                break
        if past_close is not None:
            return "Up" if latest_close >= past_close else "Down"

    prev_close = market.get("prev_close")
    price_str = market.get("price", "n/a")
    try:
        price = float(price_str.replace("$", "")) if isinstance(price_str, str) else float(price_str)
    except (TypeError, ValueError, AttributeError):
        price = None
    if prev_close is None or price is None:
        return "n/a"
    return "Up" if price >= prev_close else "Down"


def detect_event_type(entry: dict) -> str:
    resume_time = get_first(entry, ["resumetime", "resume_time", "resumetime_est", "resume_time_est"])
    resume_date = get_first(entry, ["resumedate", "resume_date"])
    reason = get_first(entry, ["reasoncode", "reason_code", "reason"]).upper()
    if resume_time or resume_date:
        return "RESUME"
    if "RESUME" in reason:
        return "RESUME"
    return "HALT"


def event_id_for(entry: dict, event_type: str) -> str:
    symbol = get_first(entry, ["symbol", "ticker", "title"]).upper()
    halt_date = get_first(entry, ["haltdate", "halt_date", "date"])
    halt_time = get_first(entry, ["halttime", "halt_time"])
    resume_date = get_first(entry, ["resumedate", "resume_date"])
    resume_time = get_first(entry, ["resumetime", "resume_time"])

    parts = [event_type, symbol, halt_date, halt_time, resume_date, resume_time]
    compact = "|".join([part for part in parts if part])
    if compact:
        return compact

    # Fallback: build a stable digest from normalized fields excluding source
    normalized = {}
    for key, value in entry.items():
        if key == "source":
            continue
        if value is None:
            continue
        normalized[key] = str(value).strip()
    digest = hashlib.sha1(json.dumps(normalized, sort_keys=True).encode("utf-8", errors="ignore")).hexdigest()
    return f"fallback:{digest}"



def fetch_rss_events() -> List[dict]:
    feed = fetch_rss(TRADE_HALTS_RSS)
    events = []
    for entry in feed.entries:
        data = {"source": "nasdaq_rss"}
        data["symbol"] = entry.get("symbol") or entry.get("ticker") or entry.get("title") or "UNKNOWN"
        summary = entry.get("summary", "")
        if summary:
            rows = parse_html_table(summary)
            if rows:
                data.update(normalize_row(rows[0]))
        data["title"] = entry.get("title")
        data["link"] = entry.get("link")
        data["published"] = entry.get("published")
        events.append(data)
    return events


def fetch_nasdaq_page_events() -> List[dict]:
    content = request_with_retries(NASDAQ_TRADE_HALTS_PAGE, timeout=20, max_attempts=2)
    html_text = content.decode("utf-8", errors="ignore")
    rows = parse_html_table(html_text)
    if not rows:
        return []
    events = []
    for row in rows:
        data = normalize_row(row)
        if data:
            data["source"] = "nasdaq_page"
            events.append(data)
    return events


def fetch_nyse_events() -> List[dict]:
    content = request_with_retries(NYSE_TRADE_HALTS_CSV, timeout=20, max_attempts=2)
    text = content.decode("utf-8", errors="ignore")
    reader = csv.DictReader(StringIO(text))
    events = []
    for row in reader:
        data = normalize_row(row)
        if data:
            data["source"] = "nyse_csv"
            events.append(data)
    return events


def fetch_trade_halts() -> List[dict]:
    try:
        events = fetch_rss_events()
        if events:
            return events
        logging.warning("RSS returned no events, trying Nasdaq page")
    except Exception as exc:
        logging.warning("RSS fetch failed: %s", exc)

    try:
        events = fetch_nasdaq_page_events()
        if events:
            return events
        logging.warning("Nasdaq page returned no events, trying NYSE CSV")
    except Exception as exc:
        logging.warning("Nasdaq page fetch failed: %s", exc)

    try:
        events = fetch_nyse_events()
        if events:
            return events
        logging.warning("NYSE CSV returned no events")
    except Exception as exc:
        logging.warning("NYSE CSV fetch failed: %s", exc)

    return []


def format_compact(value: Optional[Union[float, int]]) -> str:
    if value is None:
        return "n/a"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"

    abs_num = abs(num)
    units = [(1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")]
    for threshold, suffix in units:
        if abs_num >= threshold:
            return f"{num / threshold:.2f}{suffix}"
    return f"{num:.2f}"


def format_price(value: Optional[Union[float, int]]) -> str:
    if value is None:
        return "n/a"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"${num:.2f}"


def fetch_market_data(ticker: str) -> dict:
    api_key = os.environ.get("FMP_API_KEY")
    if api_key:
        try:
            url = FMP_QUOTE_URL.format(ticker=ticker, apikey=api_key)
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            data = response.json()
            if not data:
                raise ValueError("empty response")
            quote = data[0]
            return {
                "price": format_price(quote.get("price")),
                "market_cap": format_compact(quote.get("marketCap")),
                "float": format_compact(quote.get("sharesFloat")),
            }
        except Exception as exc:
            logging.warning("FMP market data failed for %s: %s", ticker, exc)

    av_quote = fetch_alphavantage_quote(ticker)
    price = av_quote.get("price")
    prev_close = av_quote.get("prev_close")
    if price is None:
        td_quote = fetch_twelvedata_quote(ticker)
        price = td_quote.get("price")
        if prev_close is None:
            prev_close = td_quote.get("prev_close")
    shares = fetch_sec_shares_outstanding(ticker)
    public_float = fetch_sec_public_float(ticker)
    market_cap = price * shares if price is not None and shares is not None else None
    overnight_change = None
    if prev_close and price is not None:
        try:
            overnight_change = (price - prev_close) / prev_close
        except Exception:
            overnight_change = None
    short_interest = fetch_finra_short_interest(ticker)
    return {
        "price": format_price(price),
        "market_cap": format_compact(market_cap),
        "float": format_compact(public_float),
        "prev_close": prev_close,
        "ticker": ticker,
        "overnight_change": overnight_change,
        "short_interest_shares": short_interest.get("short_interest_shares"),
        "short_interest_date": short_interest.get("short_interest_date"),
        "days_to_cover": short_interest.get("days_to_cover"),
    }


def shorten(text: str, limit: int = 80) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def fetch_news_summary(ticker: str, company_name: Optional[str] = None) -> dict:
    queries = []
    if ticker:
        queries.append(f"{ticker} stock")
    if company_name:
        queries.append(company_name)
    if ticker and company_name:
        queries.append(f"{ticker} OR \"{company_name}\"")

    for query in queries:
        url = GOOGLE_NEWS_RSS.format(query=requests.utils.quote(query))
        try:
            feed = fetch_rss(url)
            entries = feed.entries[:3]
            if not entries:
                continue

            headlines = [shorten(entry.get("title", "")) for entry in entries if entry.get("title")]
            summary = "; ".join(headlines) if headlines else "n/a"
            link = entries[0].get("link") or "n/a"
            return {"link": link, "summary": summary, "found": True}
        except Exception as exc:
            logging.warning("News fetch failed for %s: %s", query, exc)
            continue

    return {"link": "n/a", "summary": "No recent Google News results", "found": False}


def fetch_latest_tweet(ticker: str, company_name: Optional[str] = None) -> Optional[str]:
    if not X_API_BEARER_TOKEN:
        return None

    queries = []
    if ticker:
        queries.append(f"${ticker} OR {ticker}")
        queries.append(f"{ticker} stock")
        queries.append(f"{ticker} halt")
    if company_name:
        queries.append(company_name)

    headers = {"Authorization": f"Bearer {X_API_BEARER_TOKEN}"}
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - (48 * 3600)

    for query in queries:
        try:
            params = {
                "query": query,
                "max_results": 10,
                "tweet.fields": "created_at,author_id",
                "expansions": "author_id",
                "user.fields": "username",
            }
            response = requests.get(X_SEARCH_URL, headers=headers, params=params, timeout=20)
            if response.status_code == 400:
                logging.warning("X API bad request for query: %s", query)
                continue
            if response.status_code == 401:
                logging.warning("X API unauthorized for query: %s", query)
                return "__x_unauthorized__"
            if response.status_code == 429:
                logging.warning("X API rate limit reached")
                return None
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", [])
            users = {u["id"]: u.get("username") for u in payload.get("includes", {}).get("users", [])}
            if not data:
                continue
            # find most recent within 48h
            best = None
            best_ts = 0
            for tweet in data:
                created_at = tweet.get("created_at")
                if not created_at:
                    continue
                try:
                    ts = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                if ts > best_ts:
                    best_ts = ts
                    best = tweet
            if not best:
                continue
            username = users.get(best.get("author_id"), "i")
            return f"https://x.com/{username}/status/{best.get('id')}"
        except Exception as exc:
            logging.warning("X API search failed for %s: %s", query, exc)
            continue

    return None


def terminal_notifier_path() -> Optional[str]:
    env_path = os.environ.get("TERMINAL_NOTIFIER_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    path = shutil.which("terminal-notifier")
    if path:
        return path
    for candidate in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier"):
        if os.path.exists(candidate):
            return candidate
    return None


def send_notification(title: str, body: str, open_url: Optional[str] = None) -> None:
    safe_title = sanitize_for_osascript(title)
    safe_body = sanitize_for_osascript(body)
    try:
        notifier = terminal_notifier_path()
        if notifier:
            logging.info("Using terminal-notifier: %s", notifier)
            args = [
                notifier,
                "-title",
                safe_title,
                "-message",
                safe_body,
                "-sound",
                "Glass",
            ]
            if TERMINAL_NOTIFIER_SENDER:
                args.extend(["-sender", TERMINAL_NOTIFIER_SENDER])
            if TERMINAL_NOTIFIER_ACTIVATE:
                args.extend(["-activate", TERMINAL_NOTIFIER_ACTIVATE])
            if open_url and open_url.startswith("http"):
                logging.info("Notification open URL: %s", open_url)
                args.extend(["-open", open_url])
            subprocess.run(args, check=False)
        else:
            logging.info("Using osascript notifications (no terminal-notifier found)")
            script = (
                f"display notification {json.dumps(safe_body)} with title {json.dumps(safe_title)} "
                f"sound name {json.dumps('Glass')}"
            )
            subprocess.run(["osascript", "-e", script], check=False)
        if OPEN_DETAILS and open_url and open_url.startswith("http"):
            subprocess.run(["open", open_url], check=False)
    except Exception as exc:
        logging.warning("Notification failed: %s", exc)


def get_enrichment(ticker: str, company_name: Optional[str]) -> tuple[dict, dict]:
    news = fetch_news_summary(ticker, company_name)
    if not news.get("found"):
        tweet_link = fetch_latest_tweet(ticker, company_name)
        if tweet_link:
            news = {"link": tweet_link, "summary": "Latest tweet", "found": True}
        else:
            news = {"link": "n/a", "summary": "No recent news or tweets", "found": False}
    market = fetch_market_data(ticker)
    return news, market


def build_body(
    entry: dict,
    event_type: str,
    more_details: Optional[str] = None,
    news: Optional[dict] = None,
    market: Optional[dict] = None,
) -> str:
    ticker = get_first(entry, ["symbol", "ticker", "title"], "n/a")
    halt_date = get_first(entry, ["haltdate", "halt_date", "date"], "n/a")
    reason = get_first(entry, ["reasoncode", "reason_code", "reason"], "n/a")
    company_name = get_first(entry, ["name"], "")

    if news is None or market is None:
        news, market = get_enrichment(ticker, company_name)

    lines = [
        f"Ticker: {ticker}",
        f"Halt date: {halt_date}",
        f"Reason: {reason}",
        f"News: {news['link']}",
        f"News summary: {news['summary']}",
        f"Price: {market['price']}",
        f"Market cap: {market['market_cap']}",
        f"Float: {market['float']}",
    ]
    if entry.get("catalyst_label"):
        label = entry.get("catalyst_label")
        conf = entry.get("catalyst_confidence")
        if conf != "":
            try:
                conf_pct = float(conf) * 100
                lines.insert(3, f"Catalyst: {label} ({conf_pct:.0f}%)")
            except Exception:
                lines.insert(3, f"Catalyst: {label}")
        else:
            lines.insert(3, f"Catalyst: {label}")
    if market.get("overnight_change") is not None:
        pct = market["overnight_change"] * 100
        lines.append(f"Overnight change: {pct:+.2f}%")
    if market.get("short_interest_shares"):
        lines.append(f"Short interest shares: {market.get('short_interest_shares')}")
    if market.get("short_interest_date"):
        lines.append(f"Short interest date: {market.get('short_interest_date')}")
    if market.get("days_to_cover") is not None:
        lines.append(f"Days to cover: {market.get('days_to_cover')}")
    if entry.get("halt_direction"):
        lines.insert(3, f"Halt direction: {entry.get('halt_direction')}")
    if entry.get("important"):
        lines.insert(3, "Important: YES")
    if more_details:
        lines.append(f"More details: {more_details}")
    if TEST_MODE:
        lines.insert(0, "Test alert")
    if event_type == "RESUME":
        resume_date = get_first(entry, ["resumedate", "resume_date"], "n/a")
        resume_time = get_first(entry, ["resumetime", "resume_time"], "n/a")
        lines.insert(2, f"Resume: {resume_date} {resume_time}".strip())
    return "\n".join(lines)


def sanitize_for_osascript(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    return "".join(ch if ch == "\n" or ch >= " " else " " for ch in text)


def details_url(event_id: str) -> str:
    return f"http://{DETAILS_HOST}:{DETAILS_PORT}/alerts/{quote(event_id)}"


def alert_center_url(event_id: Optional[str] = None) -> str:
    base = f"http://{DETAILS_HOST}:{DETAILS_PORT}/"
    if event_id:
        return f"{base}?alert={quote(event_id)}"
    return base


def maybe_details_url(event_id: str) -> Optional[str]:
    return alert_center_url(event_id) if DETAILS_AVAILABLE else None


def render_alert_page(alert: dict) -> str:
    def render_value(value: str) -> str:
        text = html_lib.escape(str(value))
        if str(value).startswith("http://") or str(value).startswith("https://"):
            return f'<a href="{text}" target="_blank" rel="noopener noreferrer">{text}</a>'
        return text

    reason_code = str(alert.get("reason", "")).strip().upper()
    reason_desc = HALT_CODE_DESCRIPTIONS.get(reason_code)
    important = bool(alert.get("important"))

    lines = [
        (
            f"<h1 style=\"color:#c00000;\">{html_lib.escape(alert.get('title', 'Alert'))}</h1>"
            if important
            else f"<h1>{html_lib.escape(alert.get('title', 'Alert'))}</h1>"
        ),
        "<ul>",
    ]
    for key, label in [
        ("ticker", "Ticker"),
        ("company_name", "Company"),
        ("halt_date", "Halt date"),
        ("halt_time", "Halt time"),
        ("resume_date", "Resume date"),
        ("resume_time", "Resume time"),
        ("reason", "Reason"),
        ("halt_direction", "Halt direction"),
        ("news_link", "News link"),
        ("news_summary", "News summary"),
        ("price", "Price"),
        ("market_cap", "Market cap"),
        ("float", "Float"),
        ("overnight_change", "Overnight change"),
        ("short_interest_shares", "Short interest shares"),
        ("short_interest_date", "Short interest date"),
        ("days_to_cover", "Days to cover"),
        ("important", "Important"),
        ("catalyst_label", "Catalyst"),
        ("catalyst_confidence", "Catalyst confidence"),
        ("catalyst_rationale", "Catalyst rationale"),
        ("source", "Source"),
        ("event_type", "Event type"),
        ("timestamp", "Timestamp"),
    ]:
        value = alert.get(key)
        if value:
            if key == "overnight_change":
                try:
                    pct = float(value) * 100
                    lines.append(f"<li><strong>{label}:</strong> {pct:+.2f}%</li>")
                except Exception:
                    lines.append(f"<li><strong>{label}:</strong> {render_value(value)}</li>")
            elif key == "catalyst_confidence":
                try:
                    pct = float(value) * 100
                    lines.append(f"<li><strong>{label}:</strong> {pct:.0f}%</li>")
                except Exception:
                    lines.append(f"<li><strong>{label}:</strong> {render_value(value)}</li>")
            else:
                lines.append(f"<li><strong>{label}:</strong> {render_value(value)}</li>")
    if reason_desc:
        lines.append(f"<li><strong>Halt code description:</strong> {html_lib.escape(reason_desc)}</li>")
    tweet_link = fetch_latest_tweet(alert.get("ticker", ""), alert.get("company_name", ""))
    if tweet_link == "__x_unauthorized__":
        lines.append("<li><strong>Latest tweet:</strong> X API unauthorized. Check bearer token and plan.</li>")
    elif tweet_link:
        lines.append(f"<li><strong>Latest tweet:</strong> {render_value(tweet_link)}</li>")
    else:
        if X_API_BEARER_TOKEN:
            lines.append("<li><strong>Latest tweet:</strong> No recent tweet within 48 hours</li>")
        else:
            lines.append("<li><strong>Latest tweet:</strong> X_API_BEARER_TOKEN not set</li>")
    lines.append("</ul>")
    return "\n".join(lines)


def render_alert_center_page() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trade Halt Alert Center</title>
  <style>
    :root {
      color-scheme: light;
    }
    body {
      margin: 0;
      font-family: "IBM Plex Mono", "Menlo", "Monaco", monospace;
      background: #0f1115;
      color: #e7e9ee;
    }
    header {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      padding: 16px 20px;
      background: linear-gradient(135deg, #161b22, #0f1115);
      border-bottom: 1px solid #23262f;
    }
    header h1 {
      margin: 0;
      font-size: 20px;
      letter-spacing: 0.4px;
    }
    .status {
      display: flex;
      gap: 10px;
      align-items: center;
      font-size: 13px;
    }
    .pill {
      padding: 4px 10px;
      border-radius: 999px;
      background: #1f2430;
      border: 1px solid #2c313d;
    }
    .pill.paused {
      background: #3a1f1f;
      border-color: #612424;
      color: #ff8a8a;
    }
    .controls button {
      padding: 6px 12px;
      border-radius: 6px;
      border: 1px solid #2b303b;
      background: #1a1f2b;
      color: #e7e9ee;
      cursor: pointer;
      font-size: 12px;
    }
    .controls button:hover {
      background: #242a38;
    }
    .layout {
      display: grid;
      grid-template-columns: 320px 1fr;
      gap: 0;
      min-height: calc(100vh - 70px);
    }
    .sidebar {
      border-right: 1px solid #23262f;
      background: #11131a;
      display: flex;
      flex-direction: column;
    }
    .filters {
      padding: 12px;
      border-bottom: 1px solid #23262f;
      display: grid;
      gap: 8px;
    }
    .filters input, .filters select {
      width: 100%;
      padding: 6px 8px;
      background: #0c0f14;
      border: 1px solid #2b303b;
      border-radius: 6px;
      color: #e7e9ee;
      font-size: 12px;
    }
    .alert-list {
      overflow-y: auto;
      flex: 1;
    }
    .alert-card {
      padding: 12px;
      border-bottom: 1px solid #1f232c;
      cursor: pointer;
      display: grid;
      gap: 6px;
    }
    .alert-card.important {
      border-left: 4px solid #d94848;
      background: #1a1416;
    }
    .alert-card:hover {
      background: #171b25;
    }
    .alert-card .title {
      font-size: 13px;
      font-weight: 600;
    }
    .alert-card .meta {
      font-size: 11px;
      color: #a4a9b6;
    }
    .content {
      padding: 20px;
    }
    .content h2 {
      margin-top: 0;
      font-size: 22px;
    }
    .content .important-title {
      color: #d94848;
    }
    .content ul {
      padding-left: 18px;
    }
    .content a {
      color: #7db3ff;
    }
    @media (max-width: 900px) {
      .layout {
        grid-template-columns: 1fr;
      }
      .sidebar {
        border-right: none;
        border-bottom: 1px solid #23262f;
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>Trade Halt Alert Center</h1>
    <div class="status">
      <span id="agent-status" class="pill">Loading</span>
      <span id="paused-status" class="pill">Alerts</span>
    </div>
    <div class="controls">
      <button onclick="pauseAlerts()">Pause</button>
      <button onclick="resumeAlerts()">Resume</button>
      <button onclick="refreshAlerts()">Refresh</button>
    </div>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <div class="filters">
        <input id="search" placeholder="Search ticker or company" oninput="renderList()"/>
        <select id="filter-type" onchange="renderList()">
          <option value="">All events</option>
          <option value="HALT">HALT</option>
          <option value="RESUME">RESUME</option>
        </select>
        <label style="font-size:12px;color:#a4a9b6;">
          <input type="checkbox" id="filter-important" onchange="renderList()"/> Important only
        </label>
      </div>
      <div id="alert-list" class="alert-list"></div>
    </aside>
    <main class="content" id="alert-details">
      <h2>Select an alert</h2>
      <p>Use the list to view details.</p>
    </main>
  </div>
  <script>
    let alerts = [];
    let selectedId = null;

    async function refreshAlerts() {
      const response = await fetch('/api/alerts');
      alerts = await response.json();
      renderList();
      const urlParams = new URLSearchParams(window.location.search);
      const initial = urlParams.get('alert');
      if (initial) {
        selectAlert(initial);
      } else if (!selectedId && alerts.length) {
        selectAlert(alerts[0].event_id);
      }
    }

    function renderList() {
      const list = document.getElementById('alert-list');
      list.innerHTML = '';
      const query = document.getElementById('search').value.toLowerCase();
      const type = document.getElementById('filter-type').value;
      const importantOnly = document.getElementById('filter-important').checked;
      const filtered = alerts.filter(alert => {
        const text = `${alert.ticker || ''} ${alert.company_name || ''}`.toLowerCase();
        if (query && !text.includes(query)) return false;
        if (type && alert.event_type !== type) return false;
        if (importantOnly && !alert.important) return false;
        return true;
      });
      filtered.forEach(alert => {
        const card = document.createElement('div');
        card.className = 'alert-card' + (alert.important ? ' important' : '');
        card.onclick = () => selectAlert(alert.event_id);
        card.innerHTML = `
          <div class="title">${alert.title || alert.event_type}</div>
          <div class="meta">${alert.ticker || ''} • ${alert.halt_date || alert.resume_date || ''}</div>
          <div class="meta">${alert.reason || ''}</div>
        `;
        list.appendChild(card);
      });
    }

    async function selectAlert(id) {
      selectedId = id;
      history.replaceState(null, '', `/?alert=${encodeURIComponent(id)}`);
      const response = await fetch(`/api/alerts/${encodeURIComponent(id)}`);
      const alert = await response.json();
      const details = document.getElementById('alert-details');
      if (!alert.event_id) {
        details.innerHTML = '<h2>Alert not found</h2>';
        return;
      }
      const titleClass = alert.important ? 'important-title' : '';
      details.innerHTML = `
        <h2 class="${titleClass}">${alert.title || 'Alert'}</h2>
        <ul>
          ${renderDetailLine('Ticker', alert.ticker)}
          ${renderDetailLine('Company', alert.company_name)}
          ${renderDetailLine('Halt date', alert.halt_date)}
          ${renderDetailLine('Halt time', alert.halt_time)}
          ${renderDetailLine('Resume date', alert.resume_date)}
          ${renderDetailLine('Resume time', alert.resume_time)}
          ${renderDetailLine('Reason', alert.reason)}
          ${renderDetailLine('Halt direction', alert.halt_direction)}
          ${renderDetailLine('News link', alert.news_link, true)}
          ${renderDetailLine('News summary', alert.news_summary)}
          ${renderDetailLine('Price', alert.price)}
          ${renderDetailLine('Market cap', alert.market_cap)}
          ${renderDetailLine('Float', alert.float)}
          ${renderDetailLine('Overnight change', alert.overnight_change ? (alert.overnight_change * 100).toFixed(2) + '%' : '')}
          ${renderDetailLine('Short interest shares', alert.short_interest_shares)}
          ${renderDetailLine('Short interest date', alert.short_interest_date)}
          ${renderDetailLine('Days to cover', alert.days_to_cover)}
          ${renderDetailLine('Important', alert.important ? 'YES' : '')}
          ${renderDetailLine('Source', alert.source)}
          ${renderDetailLine('Event type', alert.event_type)}
          ${renderDetailLine('Timestamp', alert.timestamp)}
        </ul>
      `;
    }

    function renderDetailLine(label, value, isLink) {
      if (!value) return '';
      if (isLink && String(value).startsWith('http')) {
        return `<li><strong>${label}:</strong> <a href="${value}" target="_blank" rel="noopener noreferrer">${value}</a></li>`;
      }
      return `<li><strong>${label}:</strong> ${value}</li>`;
    }

    async function pauseAlerts() {
      await fetch('/api/pause', {method: 'POST'});
      refreshStatus();
    }

    async function resumeAlerts() {
      await fetch('/api/resume', {method: 'POST'});
      refreshStatus();
    }

    async function refreshStatus() {
      const response = await fetch('/api/status');
      const status = await response.json();
      const agent = document.getElementById('agent-status');
      const paused = document.getElementById('paused-status');
      agent.textContent = status.running ? 'Running' : 'Stopped';
      paused.textContent = status.paused ? 'Paused' : 'Active';
      paused.className = status.paused ? 'pill paused' : 'pill';
    }

    refreshAlerts();
    refreshStatus();
    setInterval(refreshAlerts, 15000);
    setInterval(refreshStatus, 10000);
  </script>
</body>
</html>
"""


def start_details_server() -> bool:
    global DETAILS_AVAILABLE
    def json_response(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        handler.send_header("Pragma", "no-cache")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def update_pause_state(paused: bool) -> None:
        state = load_state()
        state["paused"] = paused
        save_state(state)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = render_alert_center_page().encode("utf-8", errors="ignore")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/status":
                state = load_state()
                json_response(
                    self,
                    200,
                    {
                        "paused": bool(state.get("paused", False)),
                        "last_poll": state.get("last_poll", 0),
                        "alert_count": len(state.get("recent_alerts", [])),
                        "running": True,
                    },
                )
                return
            if parsed.path == "/api/alerts":
                state = load_state()
                alerts = list(reversed(state.get("recent_alerts", [])))
                json_response(self, 200, alerts)
                return
            if parsed.path.startswith("/api/alerts/"):
                alert_id = unquote(parsed.path[len("/api/alerts/"):])
                state = load_state()
                found = next((a for a in state.get("recent_alerts", []) if a.get("event_id") == alert_id), {})
                json_response(self, 200, found or {})
                return
            if not parsed.path.startswith("/alerts/"):
                self.send_response(404)
                self.end_headers()
                return
            alert_id = unquote(parsed.path[len("/alerts/"):])
            try:
                if STATE_PATH.exists():
                    data = json.loads(STATE_PATH.read_text())
                    alerts = data.get("recent_alerts", [])
                else:
                    alerts = []
            except Exception:
                alerts = []
            found = next((a for a in alerts if a.get("event_id") == alert_id), None)
            if not found:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Alert not found")
                return
            body = render_alert_page(found).encode("utf-8", errors="ignore")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            return

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/pause":
                update_pause_state(True)
                json_response(self, 200, {"paused": True})
                return
            if parsed.path == "/api/resume":
                update_pause_state(False)
                json_response(self, 200, {"paused": False})
                return
            self.send_response(404)
            self.end_headers()

    try:
        server = HTTPServer((DETAILS_HOST, DETAILS_PORT), Handler)
    except OSError as exc:
        logging.warning("Details server failed to start: %s", exc)
        DETAILS_AVAILABLE = False
        return False

    def run():
        server.serve_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logging.info("Details server listening on http://%s:%s", DETAILS_HOST, DETAILS_PORT)
    DETAILS_AVAILABLE = True
    return True


def build_scheduled_resume_body(
    pending: dict,
    more_details: Optional[str] = None,
    news: Optional[dict] = None,
    market: Optional[dict] = None,
) -> str:
    ticker = pending.get("ticker", "n/a")
    halt_date = pending.get("halt_date", "n/a")
    reason = pending.get("reason", "n/a")
    delay_minutes = pending.get("delay_minutes", "n/a")
    company_name = pending.get("company_name", "")

    if news is None or market is None:
        news, market = get_enrichment(ticker, company_name)

    lines = [
        f"Ticker: {ticker}",
        f"Halt date: {halt_date}",
        f"Reason: {reason}",
        f"Resume: scheduled after {delay_minutes} minutes",
        f"News: {news['link']}",
        f"News summary: {news['summary']}",
        f"Price: {market['price']}",
        f"Market cap: {market['market_cap']}",
        f"Float: {market['float']}",
    ]
    if more_details:
        lines.append(f"More details: {more_details}")
    if TEST_MODE:
        lines.insert(0, "Test alert")
    return "\n".join(lines)


def build_alert_record(entry: dict, event_type: str, news: dict, market: dict, event_id: str, source: str) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "ticker": get_first(entry, ["symbol", "ticker", "title"], "n/a"),
        "company_name": get_first(entry, ["name"], ""),
        "halt_date": get_first(entry, ["haltdate", "halt_date", "date"], "n/a"),
        "halt_time": get_first(entry, ["halttime", "halt_time"], ""),
        "resume_date": get_first(entry, ["resumedate", "resume_date"], ""),
        "resume_time": get_first(entry, ["resumetime", "resume_time"], ""),
        "reason": get_first(entry, ["reasoncode", "reason_code", "reason"], "n/a"),
        "halt_direction": entry.get("halt_direction", ""),
        "news_link": news.get("link", "n/a"),
        "news_summary": news.get("summary", "n/a"),
        "price": market.get("price", "n/a"),
        "market_cap": market.get("market_cap", "n/a"),
        "float": market.get("float", "n/a"),
        "overnight_change": market.get("overnight_change"),
        "short_interest_shares": market.get("short_interest_shares"),
        "short_interest_date": market.get("short_interest_date"),
        "days_to_cover": market.get("days_to_cover"),
        "catalyst_label": entry.get("catalyst_label", ""),
        "catalyst_confidence": entry.get("catalyst_confidence", ""),
        "catalyst_rationale": entry.get("catalyst_rationale", ""),
        "source": source,
        "title": f"{event_type}: {get_first(entry, ['symbol', 'ticker', 'title'], 'n/a')}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def schedule_resume(state: dict, entry: dict, event_id: str) -> None:
    ticker = get_first(entry, ["symbol", "ticker"], "UNKNOWN")
    halt_date = get_first(entry, ["haltdate", "halt_date", "date"], "n/a")
    reason = get_first(entry, ["reasoncode", "reason_code", "reason"], "n/a")
    company_name = get_first(entry, ["name"], "")

    halt_counts = state.setdefault("halt_counts", {})
    count = int(halt_counts.get(ticker, 0)) + 1
    halt_counts[ticker] = count

    if count == 1:
        delay_minutes = 5
    elif count == 2:
        delay_minutes = 10
    else:
        delay_minutes = 10

    delay_seconds = delay_minutes * 60
    if count == 1 and TEST_DELAY_FIRST:
        try:
            delay_seconds = int(TEST_DELAY_FIRST)
            delay_minutes = max(1, int(round(delay_seconds / 60)))
        except ValueError:
            logging.warning("Invalid HALT_ALERTS_TEST_DELAY_FIRST, using default")
    if count == 2 and TEST_DELAY_SECOND:
        try:
            delay_seconds = int(TEST_DELAY_SECOND)
            delay_minutes = max(1, int(round(delay_seconds / 60)))
        except ValueError:
            logging.warning("Invalid HALT_ALERTS_TEST_DELAY_SECOND, using default")

    due_at = time.time() + delay_seconds
    pending = {
        "ticker": ticker,
        "halt_date": halt_date,
        "reason": reason,
        "company_name": company_name,
        "delay_minutes": delay_minutes,
        "due_at": due_at,
        "event_id": event_id,
    }
    state.setdefault("pending_resumes", []).append(pending)


def cancel_pending_for_ticker(state: dict, ticker: str) -> None:
    pending_list = state.get("pending_resumes", [])
    state["pending_resumes"] = [p for p in pending_list if p.get("ticker") != ticker]


def process_due_resumes(state: dict) -> int:
    now = time.time()
    pending_list = state.get("pending_resumes", [])
    remaining = []
    sent = 0
    paused = state.get("paused", False)

    for pending in pending_list:
        due_at = pending.get("due_at", 0)
        if due_at and due_at <= now:
            ticker = pending.get("ticker", "UNKNOWN")
            event_id = pending.get("event_id") or f"scheduled:{ticker}:{int(due_at)}"
            if paused:
                state.setdefault("halt_counts", {})[ticker] = 0
                continue
            title = f"RESUME: {ticker}"
            if TEST_MODE:
                title = f"TEST {title}"
            more_details = maybe_details_url(event_id)
            news, market = get_enrichment(ticker, pending.get("company_name", ""))
            body = build_scheduled_resume_body(pending, more_details=more_details, news=news, market=market)
            record_alert(
                state,
                build_alert_record(
                    {
                        "symbol": ticker,
                        "haltdate": pending.get("halt_date"),
                        "reasoncode": pending.get("reason"),
                        "resumedate": "",
                        "resumetime": "",
                    },
                    "RESUME",
                    news,
                    market,
                    event_id,
                    "scheduled",
                ),
            )
            save_state(state)
            send_notification(title, body, open_url=more_details)
            logging.info("Sent scheduled RESUME for %s", ticker)
            state.setdefault("halt_counts", {})[ticker] = 0
            sent += 1
        else:
            remaining.append(pending)

    state["pending_resumes"] = remaining
    if sent:
        save_state(state)
    return sent


def process_feed(state: dict) -> int:
    seen_ids = set(state.get("seen_ids", []))
    new_count = 0
    paused = state.get("paused", False)

    new_count += process_due_resumes(state)

    entries = fetch_trade_halts()
    if not seen_ids and entries:
        for entry in entries:
            event_type = detect_event_type(entry)
            event_id = event_id_for(entry, event_type)
            seen_ids.add(event_id)
        state["seen_ids"] = list(seen_ids)
        state["last_poll"] = time.time()
        save_state(state)
        logging.info("Seeded %s existing events without notifying", len(entries))
        return new_count

    last_poll = float(state.get("last_poll", 0) or 0)
    for entry in entries:
        event_type = detect_event_type(entry)
        event_id = event_id_for(entry, event_type)
        if event_id in seen_ids:
            continue

        halt_ts = parse_halt_datetime(entry)
        if halt_ts and last_poll and halt_ts <= last_poll:
            seen_ids.add(event_id)
            continue

        if paused:
            seen_ids.add(event_id)
            continue

        ticker = get_first(entry, ["symbol", "ticker", "title"], "UNKNOWN")
        title = f"{event_type}: {ticker}"
        if TEST_MODE:
            title = f"TEST {title}"
        more_details = maybe_details_url(event_id)
        news, market = get_enrichment(ticker, get_first(entry, ["name"], ""))
        reason_code = get_first(entry, ["reasoncode", "reason_code", "reason"], "")
        halt_direction = compute_halt_direction(reason_code, market)
        if halt_direction:
            entry["halt_direction"] = halt_direction
        if event_type == "HALT" and halt_direction == "Up" and news.get("found"):
            entry["important"] = True
            title = f"IMPORTANT {title}"
        if news.get("found"):
            payload = {
                "ticker": ticker,
                "company_name": get_first(entry, ["name"], ""),
                "reason_code": reason_code,
                "halt_direction": entry.get("halt_direction", ""),
                "news_summary": news.get("summary", ""),
                "news_link": news.get("link", ""),
            }
            catalyst = fetch_ai_catalyst(state, event_id, payload)
            if catalyst:
                entry["catalyst_label"] = catalyst.get("label", "")
                entry["catalyst_confidence"] = catalyst.get("confidence", "")
                entry["catalyst_rationale"] = catalyst.get("rationale", "")
        body = build_body(entry, event_type, more_details=more_details, news=news, market=market)
        seen_ids.add(event_id)
        state["seen_ids"] = list(seen_ids)
        record_alert(
            state,
            build_alert_record(entry, event_type, news, market, event_id, entry.get("source", "unknown")),
        )
        save_state(state)
        send_notification(title, body, open_url=more_details)
        logging.info("Notified %s for %s", event_type, ticker)

        if event_type == "HALT":
            schedule_resume(state, entry, event_id)
        else:
            cancel_pending_for_ticker(state, ticker)
            state.setdefault("halt_counts", {})[ticker] = 0

        new_count += 1

    state["seen_ids"] = list(seen_ids)
    state["last_poll"] = time.time()
    if new_count:
        save_state(state)
    return new_count


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Trade halt alerts")
    parser.add_argument("--test-notify", action="store_true", help="Send a single test notification and exit")
    parser.add_argument(
        "--test-important",
        action="store_true",
        help="Send a test notification marked as important",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Print notification integration status and exit",
    )
    parser.add_argument(
        "--keep-alive-seconds",
        type=int,
        default=0,
        help="Keep the details server alive for N seconds after a test notification",
    )
    args = parser.parse_args()

    if args.self_test:
        notifier = terminal_notifier_path()
        print("terminal_notifier:", notifier or "not found")
        print("details_url:", details_url("test-alert"))
        return

    start_details_server()

    if args.test_notify or args.test_important:
        test_ticker = "CCL"
        title = f"TEST HALT: {test_ticker}"
        event_id = f"test-alert-{test_ticker}-{int(time.time())}"
        more_details = maybe_details_url(event_id)
        market = fetch_market_data(test_ticker)
        short_interest = fetch_finra_short_interest(test_ticker)
        important = bool(args.test_important)
        if important:
            title = f"IMPORTANT {title}"
        lines = [
            "Test alert",
            f"Ticker: {test_ticker}",
            "Halt date: 2026-03-31",
            "Reason: TEST",
            "News: n/a",
            "News summary: n/a",
            f"Price: {market.get('price')}",
            f"Market cap: {market.get('market_cap')}",
            f"Float: {market.get('float')}",
        ]
        if important:
            lines.insert(3, "Important: YES")
        if short_interest.get("short_interest_shares"):
            lines.append(f"Short interest shares: {short_interest.get('short_interest_shares')}")
        if short_interest.get("short_interest_date"):
            lines.append(f"Short interest date: {short_interest.get('short_interest_date')}")
        if short_interest.get("days_to_cover") is not None:
            lines.append(f"Days to cover: {short_interest.get('days_to_cover')}")
        if more_details:
            lines.append(f"More details: {more_details}")
        body = "\n".join(
            [
                *lines,
            ]
        )
        state = load_state()
        record_alert(
            state,
            {
                "event_id": event_id,
                "event_type": "HALT",
                "ticker": test_ticker,
                "company_name": "Carnival Cruise",
                "halt_date": "2026-03-31",
                "halt_time": "",
                "resume_date": "",
                "resume_time": "",
                "reason": "TEST",
                "news_link": "n/a",
                "news_summary": "n/a",
                "price": market.get("price"),
                "market_cap": market.get("market_cap"),
                "float": market.get("float"),
                "short_interest_shares": short_interest.get("short_interest_shares"),
                "short_interest_date": short_interest.get("short_interest_date"),
                "days_to_cover": short_interest.get("days_to_cover"),
                "important": important,
                "catalyst_label": "noise",
                "catalyst_confidence": 0.0,
                "catalyst_rationale": "Test alert",
                "source": "test",
                "title": title,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        save_state(state)
        send_notification(title, body, open_url=more_details)
        logging.info("Sent test notification")
        if args.keep_alive_seconds > 0:
            logging.info("Keeping details server alive for %s seconds", args.keep_alive_seconds)
            time.sleep(args.keep_alive_seconds)
        return

    logging.info("Starting trade halt alerts")
    while True:
        try:
            state = load_state()
            new_count = process_feed(state)
            if new_count:
                logging.info("Processed %s new events", new_count)
        except Exception as exc:
            logging.warning("Feed processing failed: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
