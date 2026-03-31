# Trade Halt Alerts

macOS tool that sends desktop notifications when a stock is halted or resumes, based on NasdaqTrader Trade Halts RSS. It enriches alerts with news and market data.

## Requirements
- macOS with system `python3`
- Internet access for RSS and market data
- Optional: Financial Modeling Prep API key

## Install
```bash
cd /Users/jelk/trade-halt-alerts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
Optional, for clickable notifications:
```bash
brew install terminal-notifier
```

## Run
```bash
cd /Users/jelk/trade-halt-alerts
source .venv/bin/activate
export FMP_API_KEY="your_key_here"
export ALPHAVANTAGE_API_KEY="your_key_here"
export TWELVEDATA_API_KEY="your_key_here"
export SEC_USER_AGENT="TradeHaltAlerts/1.0 (your_email@example.com)"
python3 scripts/halt_alerts.py
```

## LaunchAgent
1) Copy the plist into LaunchAgents
```bash
mkdir -p ~/Library/LaunchAgents
cp macos/com.tradehaltalerts.plist ~/Library/LaunchAgents/
```

2) Provide the API key for the agent
```bash
launchctl setenv FMP_API_KEY "your_key_here"
```

3) Load the agent
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tradehaltalerts.plist
```

4) Unload the agent
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.tradehaltalerts.plist
```

## Friendly control script
Use `scripts/alerts_ctl.sh` to start or stop alerts.
```bash
./scripts/alerts_ctl.sh start
./scripts/alerts_ctl.sh stop
./scripts/alerts_ctl.sh restart
./scripts/alerts_ctl.sh status
./scripts/alerts_ctl.sh pause
./scripts/alerts_ctl.sh resume
```

## Notes
- State is persisted in `~/.tradehaltalerts_state.json` to deduplicate alerts across restarts.
- Logs are written to `logs/halt_alerts.log` and also printed to stdout.
- If the FMP API key is missing or the API fails, price, market cap, and float show `n/a`.
- Optional fallback: set `ALPHAVANTAGE_API_KEY` to fetch price from Alpha Vantage and compute market cap using SEC shares outstanding. Float remains `n/a`.
- For SEC requests, you can set `SEC_USER_AGENT` to a descriptive value with contact info.
- Short interest is pulled from FINRA's Equity Short Interest dataset and is updated on the FINRA schedule (typically twice per month).
- For LULD halts (`LUDP`, `LUDS`, `M`), the app can infer halt direction using Twelve Data 1 minute intraday data. Alpha Vantage intraday is a fallback if available. Set `HALT_ALERTS_INTRADAY_LOOKBACK_MINUTES` (2 to 5, default 5).
- Trade halts are fetched from NasdaqTrader RSS first, then the NasdaqTrader Trade Halts page, then the NYSE CSV endpoint as a fallback.
- Events are deduplicated across all sources using a source independent event id.
- On first run, existing halts are seeded as seen to avoid a notification flood. Only new halts after startup trigger alerts.
- Google News enrichment tries multiple queries using the ticker and company name, and falls back to a clear \"No recent Google News results\" message if nothing is found.
- If Google News has no results, the app can query the X API for a recent tweet within the last 48 hours using `X_API_BEARER_TOKEN`. If neither source has results, it shows \"No recent news or tweets\".
- Each notification includes a \"More details\" link to a local details page served at `http://127.0.0.1:8787/alerts/<event_id>`. You can change the port with `HALT_ALERTS_DETAILS_PORT`.
- If `terminal-notifier` is installed, clicking the notification opens the \"More details\" link. Otherwise it falls back to `osascript` with no click action.
- If the LaunchAgent cannot find `terminal-notifier`, set `TERMINAL_NOTIFIER_PATH` to the full path, for example `/opt/homebrew/bin/terminal-notifier`.
- If notifications do not appear for `terminal-notifier`, set `TERMINAL_NOTIFIER_SENDER=com.apple.Terminal` so it uses Terminal's notification permissions. You can also set `TERMINAL_NOTIFIER_ACTIVATE=com.apple.Terminal`.
- If clicking is unreliable, set `HALT_ALERTS_OPEN_DETAILS=auto` to automatically open the details page when a notification is sent.

## Test mode
To speed up scheduled resume alerts, set these environment variables before starting the script or LaunchAgent.
```bash
export HALT_ALERTS_TEST_DELAY_FIRST=15
export HALT_ALERTS_TEST_DELAY_SECOND=30
export HALT_ALERTS_TEST_MODE=1
```
Values are in seconds and only affect the scheduled resume notifications for the first and second halt. When test mode is enabled, notifications are labeled as test alerts.

To send a single test notification without using the feed:
```bash
python3 scripts/halt_alerts.py --test-notify --keep-alive-seconds 60
```

To check notification integration status:
```bash
python3 scripts/halt_alerts.py --self-test
```

Notifications include a sound. The current sound is `Glass`.
