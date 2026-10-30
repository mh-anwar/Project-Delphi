"""Loads .env secrets and profile.yaml. Everything else imports from here.

ROOT is the code. DATA is one person's files (.env, profile.yaml, delphi.db, applied.csv, answers, screenshots):
the checkout itself, or DELPHI_HOME when several people run their own copy from one checkout (deploy.sh <name>)."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DELPHI_HOME") or ROOT).expanduser()
if not DATA.is_dir():
    raise SystemExit(f"DELPHI_HOME is {DATA}, which isn't a folder. Make it with ./deploy.sh <name>.")

for line in (DATA / ".env").read_text().splitlines() if (DATA / ".env").exists() else []:
    key, sep, value = line.partition("=")
    if sep and not key.strip().startswith("#"):
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

PROFILE = yaml.safe_load((DATA / "profile.yaml").read_text()) if (DATA / "profile.yaml").exists() else {}


def env(key, default=None):
    return os.environ.get(key) or default
