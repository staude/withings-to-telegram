#!/usr/bin/env bash
# Legt eine venv im Projektordner an und installiert Telethon.
# Idempotent: zweiter Aufruf macht ein pip install --upgrade.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -d .venv ]]; then
  /usr/bin/python3 -m venv .venv
fi

./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install --upgrade -r requirements.txt

echo
echo "Fertig. venv liegt unter: $SCRIPT_DIR/.venv"
echo "Python: $SCRIPT_DIR/.venv/bin/python"
