#!/usr/bin/env python3
import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Union

import feedparser
import requests

TRADE_HALTS_RSS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
FMP_QUOTE_URL = "https://financialmodelingprep.com/api/v3/quote/{ticker}?apikey={apikey}"

STATE_PATH = Path.home() / ".tradehaltalerts_state.json"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "halt_alerts.log"

POLL_SECONDS = 60


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
        return {"seen_ids": [], "pending_resumes": [], "halt_counts": {}}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {"seen_ids": [], "pending_resumes": [], "halt_counts": {}}
        data.setdefault("seen_ids", [])
        data.setdefault("pending_resumes", [])
        data.setdefault("halt_counts", {})
        if not isinstance(data["seen_ids"], list):
            data["seen_ids"] = []
        if not isinstance(data["pending_resumes"], list):
            data["pending_resumes"] = []
        if not isinstance(data["halt_counts"], dict):
            data["halt_counts"] = {}
        return data
    except Exception as exc:
        logging.warning("Failed to load state, starting fresh: %s", exc)
        return {"seen_ids": [], "pending_resumes": [], "halt_counts": {}}


def save_state(state: dict) -> None:
    try:
        with STATE_PATH.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
    except Exception as exc:
        logging.warning("Failed to save state: %s", exc)


def fetch_rss(url: str) -> feedparser.FeedParserDict:
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return feedparser.parse(response.content)


def get_first(entry: dict, keys: List[str], default: str = "") -> str:
    for key in keys:
        value = entry.get(key)
        if value:
            return str(value).strip()
    return default


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
    symbol = get_first(entry, ["symbol", "ticker"])
    halt_date = get_first(entry, ["haltdate", "halt_date", "date"])
    halt_time = get_first(entry, ["halttime", "halt_time"])
    resume_date = get_first(entry, ["resumedate", "resume_date"])
    resume_time = get_first(entry, ["resumetime", "resume_time"])
    reason = get_first(entry, ["reasoncode", "reason_code", "reason"])

    parts = [event_type, symbol, halt_date, halt_time, resume_date, resume_time, reason]
    compact = "|".join([part for part in parts if part])
    if compact:
        return compact

    raw = "|".join(
        [
            get_first(entry, ["id", "guid"]),
            get_first(entry, ["title"]),
            get_first(entry, ["link"]),
            get_first(entry, ["published", "updated"]),
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
    return f"fallback:{digest}"


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


def fetch_news_summary(ticker: str) -> dict:
    query = f"{ticker} stock"
    url = GOOGLE_NEWS_RSS.format(query=requests.utils.quote(query))
    try:
        feed = fetch_rss(url)
        entries = feed.entries[:3]
        if not entries:
            return {"link": "n/a", "summary": "n/a"}

        headlines = [shorten(entry.get("title", "")) for entry in entries if entry.get("title")]
        summary = "; ".join(headlines) if headlines else "n/a"
        link = entries[0].get("link") or "n/a"
        return {"link": link, "summary": summary}
    except Exception as exc:
        logging.warning("News fetch failed for %s: %s", ticker, exc)
        return {"link": "n/a", "summary": "n/a"}


def send_notification(title: str, body: str) -> None:
    script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
    try:
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception as exc:
        logging.warning("Notification failed: %s", exc)


def build_body(entry: dict, event_type: str) -> str:
    ticker = get_first(entry, ["symbol", "ticker"], "n/a")
    halt_date = get_first(entry, ["haltdate", "halt_date", "date"], "n/a")
    reason = get_first(entry, ["reasoncode", "reason_code", "reason"], "n/a")

    news = fetch_news_summary(ticker)
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
    if event_type == "RESUME":
        resume_date = get_first(entry, ["resumedate", "resume_date"], "n/a")
        resume_time = get_first(entry, ["resumetime", "resume_time"], "n/a")
        lines.insert(2, f"Resume: {resume_date} {resume_time}".strip())
    return "\n".join(lines)


def build_scheduled_resume_body(pending: dict) -> str:
    ticker = pending.get("ticker", "n/a")
    halt_date = pending.get("halt_date", "n/a")
    reason = pending.get("reason", "n/a")
    delay_minutes = pending.get("delay_minutes", "n/a")

    news = fetch_news_summary(ticker)
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
    return "\n".join(lines)


def schedule_resume(state: dict, entry: dict, event_id: str) -> None:
    ticker = get_first(entry, ["symbol", "ticker"], "UNKNOWN")
    halt_date = get_first(entry, ["haltdate", "halt_date", "date"], "n/a")
    reason = get_first(entry, ["reasoncode", "reason_code", "reason"], "n/a")

    halt_counts = state.setdefault("halt_counts", {})
    count = int(halt_counts.get(ticker, 0)) + 1
    halt_counts[ticker] = count

    if count == 1:
        delay_minutes = 5
    elif count == 2:
        delay_minutes = 10
    else:
        delay_minutes = 10

    due_at = time.time() + (delay_minutes * 60)
    pending = {
        "ticker": ticker,
        "halt_date": halt_date,
        "reason": reason,
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

    feed = fetch_rss(TRADE_HALTS_RSS)
    for entry in feed.entries:
        event_type = detect_event_type(entry)
        event_id = event_id_for(entry, event_type)
        if event_id in seen_ids:
            continue

        ticker = get_first(entry, ["symbol", "ticker"], "UNKNOWN")
        title = f"{event_type}: {ticker}"
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
    if new_count:
        save_state(state)
    return new_count


def main() -> None:
    setup_logging()
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
