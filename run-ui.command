#!/bin/bash
# macOS/Linux UI launcher: opens the GWS Exporter page in your browser.
# launcher.py fetches the program from github.com/anujchahar-lh2/gws-workspace-exporter into "app" and builds ".venv" (open launcher.py to read what it does).
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Please install Python 3.11 from python.org"; exit 1; }
python3 launcher.py ui "$@"
