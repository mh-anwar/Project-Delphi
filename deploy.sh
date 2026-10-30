#!/usr/bin/env bash
# Install (or update) Delphi as a systemd user service that runs from this checkout, starts on boot and restarts if
# it crashes. Safe to re-run after pulling changes.
#   ./deploy.sh          one person: .env, profile.yaml and the rest live in this folder (service: delphi)
#   ./deploy.sh <name>   one of several people on this server: their files live in people/<name>/, each with their
#                        own Claude key, bot and Workday password (service: delphi@<name>)
set -euo pipefail
cd "$(dirname "$0")"
DIR=$PWD
NAME=${1:-}

if [ -n "$NAME" ]; then
    [[ $NAME =~ ^[a-z0-9_-]+$ ]] || { echo "Use a lowercase name (letters, digits, - or _): ./deploy.sh krish"; exit 1; }
    export DELPHI_HOME=$DIR/people/$NAME
    UNIT=delphi@$NAME
    if [ ! -f "$DELPHI_HOME/.env" ]; then
        mkdir -p "$DELPHI_HOME"
        chmod 700 "$DELPHI_HOME"
        # Each person's phone view gets its own port (6080 is the one-person setup's).
        PORT=$(cat people/*/.env 2>/dev/null | sed -n 's/^PHONE_VIEW_PORT=\([0-9]*\).*/\1/p' | sort -n | tail -1 || true)
        cp .env.example "$DELPHI_HOME/.env"
        printf '\n# Set by deploy.sh: this person'"'"'s phone view port on this server.\nPHONE_VIEW_PORT=%s\n' \
            "$(( ${PORT:-6080} + 1 ))" >> "$DELPHI_HOME/.env"
        chmod 600 "$DELPHI_HOME/.env"
        [ -f "$DELPHI_HOME/profile.yaml" ] || cp profile.example.yaml "$DELPHI_HOME/profile.yaml"
        echo "Made $DELPHI_HOME. Fill in its .env and profile.yaml (put the resume PDFs in there too), get the chat"
        echo "id with: DELPHI_HOME=$DELPHI_HOME .venv/bin/python bot.py  (send /start, Ctrl-C), then run ./deploy.sh $NAME again."
        exit 0
    fi
    TEMPLATE=delphi@.service
    SERVICE=delphi@.service
else
    UNIT=delphi
    TEMPLATE=delphi.service
    SERVICE=delphi.service
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/playwright install chromium >/dev/null || .venv/bin/playwright install --with-deps chromium

if ! .venv/bin/python bot.py --check; then
    echo "Fix the above, then run ./deploy.sh $NAME again. (The service isn't installed until the check passes.)"
    exit 1
fi

mkdir -p ~/.config/systemd/user
.venv/bin/python -c 'import sys; print(open(sys.argv[1]).read().replace("%h/Projects/Delphi", sys.argv[2]), end="")' \
    "$TEMPLATE" "$DIR" > ~/.config/systemd/user/"$SERVICE"
systemctl --user daemon-reload
systemctl --user enable "$UNIT" >/dev/null
systemctl --user restart "$UNIT"
loginctl enable-linger "$USER" 2>/dev/null || echo "Couldn't enable lingering: run 'sudo loginctl enable-linger $USER' so it keeps running after you log out."
sleep 3
systemctl --user --no-pager status "$UNIT" | head -5
echo "Running. Logs: journalctl --user -u $UNIT -f"
