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
TERMINAL_NOTIFIER_SENDER = os.environ.get("TERMINAL_NOTIFIER_SENDER")
TERMINAL_NOTIFIER_ACTIVATE = os.environ.get("TERMINAL_NOTIFIER_ACTIVATE")
OPEN_DETAILS = os.environ.get("HALT_ALERTS_OPEN_DETAILS", "").lower() == "auto"
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "TradeHaltAlerts/1.0")
INTRADAY_LOOKBACK_MINUTES = int(os.environ.get("HALT_ALERTS_INTRADAY_LOOKBACK_MINUTES", "5"))

ALPHAVANTAGE_QUOTE_URL = "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={ticker}&apikey={apikey}"
ALPHAVANTAGE_INTRADAY_URL = (
    "https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY&symbol={ticker}"
    "&interval=1min&outputsize=compact&apikey={apikey}"
)
TWELVEDATA_INTRADAY_URL = (
    "https://api.twelvedata.com/time_series?symbol={ticker}&interval=1min&outputsize=30&apikey={apikey}"
)
SEC_TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

_SEC_TICKER_CIK_CACHE = {"data": {}, "timestamp": 0.0}
_AV_INTRADAY_CACHE = {}
_TD_INTRADAY_CACHE = {}

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


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "seen_ids": [],
            "pending_resumes": [],
            "halt_counts": {},
            "last_poll": 0,
            "recent_alerts": [],
            "paused": False,
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
            }
        data.setdefault("seen_ids", [])
        data.setdefault("pending_resumes", [])
        data.setdefault("halt_counts", {})
        data.setdefault("last_poll", 0)
        data.setdefault("recent_alerts", [])
        data.setdefault("paused", False)
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
        }


def save_state(state: dict) -> None:
    try:
        with STATE_PATH.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
    except Exception as exc:
        logging.warning("Failed to save state: %s", exc)


def record_alert(state: dict, alert: dict) -> None:
    alerts = state.setdefault("recent_alerts", [])
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
        response = requests.get(SEC_TICKER_CIK_URL, headers={"User-Agent": SEC_USER_AGENT}, timeout=20)
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


def fetch_sec_shares_outstanding(ticker: str) -> Optional[float]:
    try:
        cik_map = fetch_sec_ticker_cik_map()
        cik = cik_map.get(ticker.upper())
        if not cik:
            return None
        url = SEC_COMPANY_FACTS_URL.format(cik=cik)
        response = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=20)
        response.raise_for_status()
        data = response.json()
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
    shares = fetch_sec_shares_outstanding(ticker)
    market_cap = price * shares if price is not None and shares is not None else None
    return {
        "price": format_price(price),
        "market_cap": format_compact(market_cap),
        "float": "n/a",
        "prev_close": prev_close,
        "ticker": ticker,
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
                args.extend(["-open", open_url, "-execute", f"open {open_url}"])
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
    if entry.get("halt_direction"):
        lines.insert(3, f"Halt direction: {entry.get('halt_direction')}")
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


def render_alert_page(alert: dict) -> str:
    def render_value(value: str) -> str:
        text = html_lib.escape(str(value))
        if str(value).startswith("http://") or str(value).startswith("https://"):
            return f'<a href="{text}" target="_blank" rel="noopener noreferrer">{text}</a>'
        return text

    reason_code = str(alert.get("reason", "")).strip().upper()
    reason_desc = HALT_CODE_DESCRIPTIONS.get(reason_code)

    lines = [
        f"<h1>{html_lib.escape(alert.get('title', 'Alert'))}</h1>",
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
        ("source", "Source"),
        ("event_type", "Event type"),
        ("timestamp", "Timestamp"),
    ]:
        value = alert.get(key)
        if value:
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


def start_details_server() -> bool:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
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
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            return

    try:
        server = HTTPServer((DETAILS_HOST, DETAILS_PORT), Handler)
    except OSError as exc:
        logging.warning("Details server failed to start: %s", exc)
        return False

    def run():
        server.serve_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logging.info("Details server listening on http://%s:%s", DETAILS_HOST, DETAILS_PORT)
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
            more_details = details_url(event_id)
            news, market = get_enrichment(ticker, pending.get("company_name", ""))
            body = build_scheduled_resume_body(pending, more_details=more_details, news=news, market=market)
            send_notification(title, body, open_url=more_details)
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
        more_details = details_url(event_id)
        news, market = get_enrichment(ticker, get_first(entry, ["name"], ""))
        reason_code = get_first(entry, ["reasoncode", "reason_code", "reason"], "")
        halt_direction = compute_halt_direction(reason_code, market)
        if halt_direction:
            entry["halt_direction"] = halt_direction
        body = build_body(entry, event_type, more_details=more_details, news=news, market=market)

        send_notification(title, body, open_url=more_details)
        record_alert(
            state,
            build_alert_record(entry, event_type, news, market, event_id, entry.get("source", "unknown")),
        )
        logging.info("Notified %s for %s", event_type, ticker)

        if event_type == "HALT":
            schedule_resume(state, entry, event_id)
        else:
            cancel_pending_for_ticker(state, ticker)
            state.setdefault("halt_counts", {})[ticker] = 0

        seen_ids.add(event_id)
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

    if args.test_notify:
        title = "TEST HALT: DEMO"
        more_details = details_url("test-alert")
        body = "\n".join(
            [
                "Test alert",
                "Ticker: TEST",
                "Halt date: 2026-03-26",
                "Reason: TEST",
                "News: n/a",
                "News summary: n/a",
                "Price: n/a",
                "Market cap: n/a",
                "Float: n/a",
                f"More details: {more_details}",
            ]
        )
        send_notification(title, body, open_url=more_details)
        state = load_state()
        record_alert(
            state,
            {
                "event_id": "test-alert",
                "event_type": "HALT",
                "ticker": "TEST",
                "company_name": "Test Company",
                "halt_date": "2026-03-26",
                "halt_time": "",
                "resume_date": "",
                "resume_time": "",
                "reason": "TEST",
                "news_link": "n/a",
                "news_summary": "n/a",
                "price": "n/a",
                "market_cap": "n/a",
                "float": "n/a",
                "source": "test",
                "title": title,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        save_state(state)
        logging.info("Sent test notification")
        if args.keep_alive_seconds > 0:
            logging.info("Keeping details server alive for %s seconds", args.keep_alive_seconds)
            time.sleep(args.keep_alive_seconds)
        return

    logging.info("Starting trade halt alerts")
    state = load_state()

    while True:
        try:
            new_count = process_feed(state)
            if new_count:
                logging.info("Processed %s new events", new_count)
        except Exception as exc:
            logging.warning("Feed processing failed: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
