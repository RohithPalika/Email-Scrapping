import os
import sys
import webbrowser
import threading
import time
import argparse

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WS2_DIR = os.path.join(PROJECT_ROOT, "WS2.0")

if WS2_DIR not in sys.path:
    sys.path.insert(0, WS2_DIR)

os.chdir(WS2_DIR)

from app import app, find_available_port  # type: ignore  # noqa: E402


def _open_browser(port):
    time.sleep(1.0)
    url = f"http://127.0.0.1:{port}"
    try:
        if sys.platform == "win32":
            os.system(f'start "" "{url}"')
        else:
            webbrowser.open(url)
    except Exception:
        try:
            webbrowser.open(url)
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PULSUS AI | Academic Intelligence Suite")
    parser.add_argument("--port", "-p", type=int, default=None, help="Port to bind (e.g. 5000, 5001)")
    parser.add_argument("pos_port", nargs="?", type=int, default=None, help="Optional positional port")
    args, _ = parser.parse_known_args()

    requested_port = args.port or args.pos_port
    if requested_port:
        bind_port = find_available_port(start_port=requested_port, max_attempts=20)
    else:
        bind_port = find_available_port(start_port=5000, max_attempts=50)

    print("=" * 65)
    print(" [*] PULSUS AI | ScholarLead Matrix (WS2.0)")
    print(f" [>] Running on: http://127.0.0.1:{bind_port}")
    print(f" [>] Process Port: {bind_port} | Shared Master Database Connected")
    print(" Opening browser automatically...")
    print("=" * 65)

    threading.Thread(target=_open_browser, args=(bind_port,), daemon=True).start()
    app.run(host="127.0.0.1", port=bind_port, debug=False, use_reloader=False, threaded=True)

