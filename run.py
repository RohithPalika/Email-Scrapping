"""Root entry point for Email Scraping application.
Allows running `python run.py` directly from the project root.
"""
import os
import sys
import runpy

if __name__ == "__main__":
    ws2_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "WS2.0")
    if ws2_dir not in sys.path:
        sys.path.insert(0, ws2_dir)
    os.chdir(ws2_dir)
    runpy.run_path(os.path.join(ws2_dir, "app.py"), run_name="__main__")
