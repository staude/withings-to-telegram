#!/usr/bin/env python3
"""Holt neue Gewichts-Messungen aus Withings und schickt sie via Telegram-User-API
(MTProto / Telethon) unter dem eigenen User-Account an einen Ziel-Bot oder -Chat.

Verwendet die User-API statt der Bot-API, damit die Nachricht von Frank persönlich
ausgeht — fremde Bots ignorieren Nachrichten anderer Bots.

- Liest .env, state.json und telegram.session aus dem Skript-Verzeichnis.
- Tauscht den Withings-Refresh-Token gegen einen neuen Access-Token + neuen Refresh-Token.
- Holt alle neuen Messungen vom Typ 1 (Gewicht) seit last_processed_ts.
- Schickt jede neue Messung als '/gewicht XX,X' an TELEGRAM_TARGET.
- Aktualisiert state.json nur bei erfolgreichem Send.

Aufruf:
    .venv/bin/python withings_to_telegram.py            # echter Lauf
    .venv/bin/python withings_to_telegram.py --dry-run  # nur listen, kein Send

Exit-Codes:
    0  Erfolg (auch wenn keine neuen Messungen vorlagen)
    1  Konfigurationsfehler
    2  API-Fehler (Withings oder Telegram)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

try:
    from telethon import TelegramClient
except ImportError:
    sys.exit("Telethon nicht installiert. Zuerst 'bash setup_venv.sh' laufen lassen "
             "und dieses Skript mit .venv/bin/python aufrufen.")

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
STATE_PATH = SCRIPT_DIR / "state.json"
LOG_PATH = SCRIPT_DIR / "withings_to_telegram.log"
SESSION_PATH = SCRIPT_DIR / "telegram"  # Telethon hängt .session selbst an

TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"

MEASTYPE_WEIGHT = 1  # Withings: Gewicht in kg


def setup_logging() -> None:
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.getLogger().addHandler(console)
    # Telethon ist gesprächig, nur Warnungen aufwärts durchlassen.
    logging.getLogger("telethon").setLevel(logging.WARNING)


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f".env nicht gefunden: {path}")
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


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        sys.exit("state.json fehlt — zuerst init_oauth.py laufen lassen.")
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"state.json kaputt: {exc}")


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    payload = urllib.parse.urlencode(fields).encode("ascii")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"HTTP-Fehler bei {url}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Keine JSON-Antwort von {url}: {raw[:200]}") from exc


def refresh_tokens(client_id: str, consumer_secret: str, refresh_token: str) -> tuple[str, str]:
    data = post_form(
        TOKEN_URL,
        {
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": consumer_secret,
            "refresh_token": refresh_token,
        },
    )
    if data.get("status") != 0:
        raise RuntimeError(f"Withings Token-Refresh fehlgeschlagen: {data}")
    body = data["body"]
    return body["access_token"], body["refresh_token"]


def fetch_measurements(access_token: str, last_processed_ts: int) -> list[dict[str, Any]]:
    # Withings: lastupdate begrenzt nach Update-Zeitpunkt, nicht Mess-Datum,
    # daher Sicherheitspuffer (10 Minuten) abziehen — wir filtern unten nochmal selbst.
    lastupdate = max(0, last_processed_ts - 600)
    data = post_form(
        MEASURE_URL,
        {
            "action": "getmeas",
            "access_token": access_token,
            "meastype": str(MEASTYPE_WEIGHT),
            "category": "1",  # 1 = echte Messung, 2 = Ziel
            "lastupdate": str(lastupdate),
        },
    )
    if data.get("status") != 0:
        raise RuntimeError(f"Withings getmeas fehlgeschlagen: {data}")
    return data["body"].get("measuregrps", [])


def extract_weight_kg(measuregrp: dict[str, Any]) -> float | None:
    for measure in measuregrp.get("measures", []):
        if measure.get("type") == MEASTYPE_WEIGHT:
            value = measure.get("value")
            unit = measure.get("unit", 0)
            if value is None:
                return None
            return float(value) * (10 ** int(unit))
    return None


def format_weight_message(weight_kg: float) -> str:
    # eine Nachkommastelle, deutsches Komma
    rounded = round(weight_kg, 1)
    return f"/gewicht {rounded:.1f}".replace(".", ",")


def resolve_target(target_raw: str) -> str | int:
    target_raw = target_raw.strip()
    if target_raw.lstrip("-").isdigit():
        return int(target_raw)
    return target_raw if target_raw.startswith("@") else "@" + target_raw


async def send_messages_via_user(
    api_id: int,
    api_hash: str,
    target: str | int,
    messages: list[str],
) -> None:
    """Schickt eine Folge von Texten unter dem eigenen User-Account.

    Nutzt die persistente Session-Datei. Wenn die nicht existiert oder abgelaufen
    ist, schlägt der Aufruf fehl — dann muss init_telegram.py erneut laufen.
    """
    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telegram-Session nicht autorisiert. "
                "init_telegram.py einmal interaktiv laufen lassen."
            )
        for text in messages:
            await client.send_message(target, text)
    finally:
        await client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Nichts an Telegram senden, nur listen.")
    args = parser.parse_args()

    setup_logging()
    logging.info("Lauf gestartet (dry_run=%s)", args.dry_run)

    env = load_env(ENV_PATH)
    client_id = require(env, "WITHINGS_CLIENT_ID")
    consumer_secret = require(env, "WITHINGS_CONSUMER_SECRET")
    api_id = int(require(env, "TELEGRAM_API_ID"))
    api_hash = require(env, "TELEGRAM_API_HASH")
    target = resolve_target(require(env, "TELEGRAM_TARGET"))

    state = load_state()
    old_refresh_token = state.get("refresh_token", "")
    if not old_refresh_token:
        sys.exit("state.json hat keinen refresh_token — init_oauth.py erneut laufen lassen.")
    last_processed_ts = int(state.get("last_processed_ts", 0))

    try:
        access_token, new_refresh_token = refresh_tokens(client_id, consumer_secret, old_refresh_token)
    except RuntimeError as exc:
        logging.error("Token-Refresh: %s", exc)
        return 2

    # Neuen Refresh-Token sofort persistieren — Withings rotiert bei jedem Refresh.
    state["refresh_token"] = new_refresh_token
    save_state(state)

    try:
        measuregrps = fetch_measurements(access_token, last_processed_ts)
    except RuntimeError as exc:
        logging.error("Messungen holen: %s", exc)
        return 2

    new_entries: list[tuple[int, float]] = []
    for grp in measuregrps:
        ts = int(grp.get("date", 0))
        if ts <= last_processed_ts:
            continue
        weight = extract_weight_kg(grp)
        if weight is None:
            continue
        new_entries.append((ts, weight))

    new_entries.sort(key=lambda t: t[0])

    if not new_entries:
        logging.info("Keine neuen Gewichts-Messungen.")
        return 0

    logging.info("%d neue Messung(en) gefunden.", len(new_entries))

    messages: list[tuple[int, str]] = []
    for ts, weight in new_entries:
        msg = format_weight_message(weight)
        when = dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
        messages.append((ts, msg))
        logging.info("Vorbereitet: %s (gemessen %s)", msg, when)

    if args.dry_run:
        for ts, msg in messages:
            logging.info("DRY-RUN: würde senden -> %s", msg)
        # Im Dry-Run trotzdem den State hochziehen, damit man nicht versehentlich
        # alte Messungen doppelt sieht. Wer den State frisch will, löscht ihn selbst.
        state["last_processed_ts"] = messages[-1][0]
        save_state(state)
        return 0

    try:
        asyncio.run(send_messages_via_user(api_id, api_hash, target, [m for _, m in messages]))
    except Exception as exc:  # noqa: BLE001 — wir wollen jeden Telethon-Fehler abfangen
        logging.error("Telegram-Send via User-API: %s", exc)
        # last_processed_ts wird NICHT erhöht — alle Messungen werden beim nächsten Lauf neu versucht.
        return 2

    state["last_processed_ts"] = messages[-1][0]
    save_state(state)
    for _, msg in messages:
        logging.info("Gesendet: %s", msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
