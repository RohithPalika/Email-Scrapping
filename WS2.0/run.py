"""Entry point for running the Flask server inside WS2.0."""
import os
import sys
import runpy

if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    os.chdir(current_dir)
    runpy.run_path(os.path.join(current_dir, "app.py"), run_name="__main__")
