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
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import List, Optional, Union

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
        return {"seen_ids": [], "pending_resumes": [], "halt_counts": {}, "last_poll": 0}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {"seen_ids": [], "pending_resumes": [], "halt_counts": {}, "last_poll": 0}
        data.setdefault("seen_ids", [])
        data.setdefault("pending_resumes", [])
        data.setdefault("halt_counts", {})
        data.setdefault("last_poll", 0)
        if not isinstance(data["seen_ids"], list):
            data["seen_ids"] = []
        if not isinstance(data["pending_resumes"], list):
            data["pending_resumes"] = []
        if not isinstance(data["halt_counts"], dict):
            data["halt_counts"] = {}
        return data
    except Exception as exc:
        logging.warning("Failed to load state, starting fresh: %s", exc)
        return {"seen_ids": [], "pending_resumes": [], "halt_counts": {}, "last_poll": 0}


def save_state(state: dict) -> None:
    try:
        with STATE_PATH.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
    except Exception as exc:
        logging.warning("Failed to save state: %s", exc)


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
    if not api_key:
        return {"price": "n/a", "market_cap": "n/a", "float": "n/a"}

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
        logging.warning("Market data failed for %s: %s", ticker, exc)
        return {"price": "n/a", "market_cap": "n/a", "float": "n/a"}


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
        queries.append(f"${ticker} OR cashtag:{ticker}")
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


def send_notification(title: str, body: str) -> None:
    safe_title = sanitize_for_osascript(title)
    safe_body = sanitize_for_osascript(body)
    script = (
        f"display notification {json.dumps(safe_body)} with title {json.dumps(safe_title)} "
        f"sound name {json.dumps('Glass')}"
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception as exc:
        logging.warning("Notification failed: %s", exc)


def build_body(entry: dict, event_type: str) -> str:
    ticker = get_first(entry, ["symbol", "ticker", "title"], "n/a")
    halt_date = get_first(entry, ["haltdate", "halt_date", "date"], "n/a")
    reason = get_first(entry, ["reasoncode", "reason_code", "reason"], "n/a")
    company_name = get_first(entry, ["name"], "")

    news = fetch_news_summary(ticker, company_name)
    tweet_link = None
    if not news.get("found"):
        tweet_link = fetch_latest_tweet(ticker, company_name)
        if tweet_link:
            news = {"link": tweet_link, "summary": "Latest tweet", "found": True}
        else:
            news = {"link": "n/a", "summary": "No recent news or tweets", "found": False}
    market = fetch_market_data(ticker)

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


def build_scheduled_resume_body(pending: dict) -> str:
    ticker = pending.get("ticker", "n/a")
    halt_date = pending.get("halt_date", "n/a")
    reason = pending.get("reason", "n/a")
    delay_minutes = pending.get("delay_minutes", "n/a")
    company_name = pending.get("company_name", "")

    news = fetch_news_summary(ticker, company_name)
    tweet_link = None
    if not news.get("found"):
        tweet_link = fetch_latest_tweet(ticker, company_name)
        if tweet_link:
            news = {"link": tweet_link, "summary": "Latest tweet", "found": True}
        else:
            news = {"link": "n/a", "summary": "No recent news or tweets", "found": False}
    market = fetch_market_data(ticker)

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
    if TEST_MODE:
        lines.insert(0, "Test alert")
    return "\n".join(lines)


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

    for pending in pending_list:
        due_at = pending.get("due_at", 0)
        if due_at and due_at <= now:
            ticker = pending.get("ticker", "UNKNOWN")
            title = f"RESUME: {ticker}"
            if TEST_MODE:
                title = f"TEST {title}"
            body = build_scheduled_resume_body(pending)
            send_notification(title, body)
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

        ticker = get_first(entry, ["symbol", "ticker"], "UNKNOWN")
        title = f"{event_type}: {ticker}"
        if TEST_MODE:
            title = f"TEST {title}"
        body = build_body(entry, event_type)

        send_notification(title, body)
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
    args = parser.parse_args()

    if args.test_notify:
        title = "TEST HALT: DEMO"
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
            ]
        )
        send_notification(title, body)
        logging.info("Sent test notification")
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
