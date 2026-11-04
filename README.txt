GWS Exporter launcher
=====================
Put next to run.command (or run.bat on Windows):
  service_account.json   the service-account key (domain-wide delegation granted)
  .env                   optional: copy .env.example (API keys, S3, Hetzner)

Web page: Windows - double-click run-ui.bat (nothing to install first; the first start downloads
uv, Python 3.11 and the packages). Mac - run-ui.command. Upload the key, fill the form, Start.
It runs users one at a time with --user, shows the live log, and lists the output.

Command line (first start downloads the program and installs its packages, a few minutes):
  ./run.command --admin admin@domain.com --user person@domain.com --export-only --local-only
  ./run.command --admin admin@domain.com --user person@domain.com --count-files-only
  ./run.command --help

Use --user EMAIL, not --only: --only lists the org through the Admin SDK and needs extra
scopes, usually failing with unauthorized_client.
Re-running the same command resumes. Results: ./output (or DataLabs/outputs/gws-exporter).
Pin a version: first line of ref.txt = tag or commit. Reset: delete "app" and/or ".venv".
The repo's own web UI (server.py) is not used: it hard-codes system python3, --only, and a
code-folder output that an update would wipe. ui.py here replaces it.
