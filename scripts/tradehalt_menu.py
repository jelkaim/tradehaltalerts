#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path

import rumps

STATE_PATH = Path.home() / ".tradehaltalerts_state.json"
DETAILS_PORT = int((__import__("os").environ.get("HALT_ALERTS_DETAILS_PORT") or "8787"))


def alert_center_url() -> str:
    return f"http://127.0.0.1:{DETAILS_PORT}/"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def set_paused(value: bool) -> None:
    state = load_state()
    state["paused"] = value
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def open_url(url: str) -> None:
    subprocess.run(["open", url], check=False)


class TradeHaltMenu(rumps.App):
    def __init__(self):
        super().__init__("THA", quit_button=None)
        self.status_item = rumps.MenuItem("Status: Loading")
        self.menu = [
            self.status_item,
            rumps.MenuItem("Open Alert Center", callback=self.open_center),
            rumps.MenuItem("Open Latest Alert", callback=self.open_latest),
            rumps.MenuItem("Pause Alerts", callback=self.pause_alerts),
            rumps.MenuItem("Resume Alerts", callback=self.resume_alerts),
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]
        self.timer = rumps.Timer(self.refresh_status, 5)
        self.timer.start()
        self.refresh_status(None)

    def refresh_status(self, _):
        state = load_state()
        paused = bool(state.get("paused", False))
        status = "Paused" if paused else "Active"
        self.status_item.title = f"Status: {status}"
        self.title = "THA" if not paused else "THA II"

    def open_center(self, _):
        open_url(alert_center_url())

    def open_latest(self, _):
        state = load_state()
        alerts = state.get("recent_alerts", [])
        if not alerts:
            return
        event_id = alerts[-1].get("event_id")
        if not event_id:
            return
        open_url(f"{alert_center_url()}?alert={event_id}")

    def pause_alerts(self, _):
        set_paused(True)
        self.refresh_status(None)

    def resume_alerts(self, _):
        set_paused(False)
        self.refresh_status(None)

    def quit_app(self, _):
        rumps.quit_application()


if __name__ == "__main__":
    TradeHaltMenu().run()
