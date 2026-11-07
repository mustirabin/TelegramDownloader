"""Entry point: `tgdl` (or `python -m tgdl`)."""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .web import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="tgdl", description="Telegram video downloader + library web UI")
    ap.add_argument("--data-dir", default="data", help="settings, Telegram session and catalog (default: ./data)")
    ap.add_argument("--port", type=int, default=None, help="web UI port (default from settings: 8765)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the web UI automatically")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("telethon").setLevel(logging.WARNING)

    data_dir = Path(args.data_dir).resolve()
    app = create_app(data_dir)
    port = args.port or app.state.app.store.settings.ui_port
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  TelegramDownloader UI: {url}\n  data dir: {data_dir}\n  library : {app.state.app.library}\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
