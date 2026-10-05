import os
import sys
import webbrowser
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WS2_DIR = os.path.join(PROJECT_ROOT, "WS2.0")

if WS2_DIR not in sys.path:
    sys.path.insert(0, WS2_DIR)

os.chdir(WS2_DIR)

from app import app  # type: ignore  # noqa: E402


def _open_browser():
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    print("=" * 60)
    print(" Academic Email Scraping & Research Outreach Tool (WS2.0)")
    print(" Running on: http://127.0.0.1:5000")
    print("=" * 60)
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=True)
