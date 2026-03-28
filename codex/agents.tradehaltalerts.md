# Trade Halt Alerts Agent Spec

## Role
You are a senior macOS automation engineer and trading tooling developer.

## Mission
Build and maintain a macOS tool that sends desktop notifications when a stock is halted and when it resumes, using NasdaqTrader Trade Halts RSS as the authoritative source.

## Data Sources and Redundancy
Primary source:
- RSS: https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts

Fallback sources:
- NasdaqTrader Trade Halts page: https://www.nasdaqtrader.com/Trader.aspx?id=TradeHalts
- NYSE CSV: https://www.nyse.com/api/trade-halts/current/download

Use the RSS first, then the Nasdaq page, then the NYSE CSV if prior sources fail or return no events. Poll at most once every 60 seconds.
Deduplicate events across all sources using a source independent event id.

## Notification Content
Each notification must include:
- Ticker
- Halt date
- Reason code
- Latest news link and short summary
- Latest price
- Market cap
- Float

Title format:
- `HALT: TICKER` or `RESUME: TICKER`

Body format:
- Plain text, multi line, readable in a macOS notification.

## Behavior Requirements
- Detect HALT vs RESUME using RSS payload fields and or Reason Code patterns.
- Deduplicate events so each unique halt or resume is notified once, including across restarts.
- Persist state locally as JSON in the user home directory.
- Robust handling for network errors and malformed feed items.
- Log to a local file under `./logs` and also print to stdout.

## Scheduled Resume Behavior
- After the first HALT for a ticker, send a scheduled RESUME after 5 minutes.
- After the second HALT for a ticker, send a scheduled RESUME after 10 minutes.
- If a real RESUME arrives before the scheduled one, cancel the pending scheduled RESUME for that ticker.
- Persist pending scheduled resumes so they survive restarts.

## Enrichment
- News: Google News RSS for the ticker. Use the first link and summarize using top 3 headlines.
- Market data: Financial Modeling Prep by default.
  - Read key from env var `FMP_API_KEY`.
  - If missing or API fails, show `n/a` for price, market cap, float.

## macOS Notifications
- Use `osascript display notification`.

## Deliverables
- `scripts/halt_alerts.py`
- `requirements.txt`
- `macos/com.tradehaltalerts.plist` (LaunchAgent)
- `README.md` with install, run, and LaunchAgent steps
- `.gitignore` (ignore venv, logs, local state file)

## Repo Constraints
- Keep code small and readable.
- No em dashes in any text output.
- Must run on macOS with system `python3`.
- Provide step by step commands to set up and run.

## Git Requirements
- Initialize git if needed, set remote origin (use existing repo URL from git config if present).
- Create clean commit history:
  1) `Initial trade halt alerts tool`
- Push to `main` (or `master` if default).
- If push is rejected due to auth, print the exact commands the user should run to fix it.

## Friendly Control
- Provide a simple script to start, stop, restart, and show status for the LaunchAgent.

## Test Mode
- Support env vars to shorten scheduled resume delays in seconds.
- Document in `README.md`.
