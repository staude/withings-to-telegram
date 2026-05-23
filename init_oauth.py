#!/usr/bin/env python3
"""Einmaliger OAuth-Flow für Withings.

Startet einen lokalen HTTP-Server, öffnet die Withings-Auth-URL im Browser,
empfängt den Code auf dem Callback, tauscht ihn gegen Access- und Refresh-Token
und schreibt den Refresh-Token in state.json.

Aufruf:
    python3 init_oauth.py

Erfordert eine ausgefüllte .env im selben Verzeichnis.
"""

from __future__ import annotations

import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
STATE_PATH = SCRIPT_DIR / "state.json"

AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
SCOPE = "user.metrics"


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f".env nicht gefunden: {path}. Vorlage .env.template kopieren und ausfüllen.")
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def require(env: dict[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        sys.exit(f"Pflichtfeld {key} fehlt in .env")
    return value


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    received: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler convention)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = dict(urllib.parse.parse_qsl(parsed.query))
        CallbackHandler.received.update(query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<html><body style='font-family:sans-serif;padding:2em;'>"
            "<h2>Withings-Authentifizierung empfangen.</h2>"
            "<p>Das Fenster kann geschlossen werden — das Init-Skript läuft weiter im Terminal.</p>"
            "</body></html>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args) -> None:  # noqa: A002, ANN001 (signature fixed by stdlib)
        return  # leise


def wait_for_callback(port: int, timeout: int = 300) -> dict[str, str]:
    server = http.server.HTTPServer(("127.0.0.1", port), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # auf Callback warten
        import time

        start = time.monotonic()
        while not CallbackHandler.received and time.monotonic() - start < timeout:
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()
    return dict(CallbackHandler.received)


def exchange_code(
    client_id: str,
    consumer_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    payload = urllib.parse.urlencode(
        {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": consumer_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    ).encode("ascii")
    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        sys.exit(f"Token-Tausch fehlgeschlagen: {exc}")
    data = json.loads(raw)
    if data.get("status") != 0:
        sys.exit(f"Withings meldet Fehler beim Token-Tausch: {data}")
    return data["body"]


def write_state(refresh_token: str) -> None:
    state = {"refresh_token": refresh_token, "last_processed_ts": 0}
    if STATE_PATH.exists():
        try:
            existing = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        existing.update(state)
        state = existing
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    env = load_env(ENV_PATH)
    client_id = require(env, "WITHINGS_CLIENT_ID")
    consumer_secret = require(env, "WITHINGS_CONSUMER_SECRET")
    redirect_uri = require(env, "WITHINGS_REDIRECT_URI")
    port = int(env.get("INIT_OAUTH_PORT", "8765"))

    state_token = secrets.token_urlsafe(16)
    auth_url = build_auth_url(client_id, redirect_uri, state_token)

    print("Öffne Withings-Authentifizierung im Browser …")
    print(f"Falls der Browser nicht aufgeht, manuell besuchen: {auth_url}")
    print(f"Lokaler Empfangs-Server hört auf http://127.0.0.1:{port}/callback")
    webbrowser.open(auth_url)

    result = wait_for_callback(port)
    if not result:
        sys.exit("Kein Callback innerhalb des Timeouts empfangen.")
    if result.get("state") != state_token:
        sys.exit("State-Token stimmt nicht überein — Abbruch (möglicher CSRF).")
    if "code" not in result:
        sys.exit(f"Withings hat keinen Code geliefert: {result}")

    body = exchange_code(client_id, consumer_secret, result["code"], redirect_uri)
    write_state(body["refresh_token"])
    print("Refresh-Token gespeichert in", STATE_PATH)
    print("Userid:", body.get("userid"))
    print("Setup abgeschlossen — withings_to_telegram.py kann jetzt laufen.")


if __name__ == "__main__":
    main()
