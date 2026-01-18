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

## Run
```bash
cd /Users/jelk/trade-halt-alerts
source .venv/bin/activate
export FMP_API_KEY="your_key_here"
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
```

## Notes
- State is persisted in `~/.tradehaltalerts_state.json` to deduplicate alerts across restarts.
- Logs are written to `logs/halt_alerts.log` and also printed to stdout.
- If the FMP API key is missing or the API fails, price, market cap, and float show `n/a`.
