#!/usr/bin/env python3
"""Einmaliger Telegram-User-Login via MTProto / Telethon.

Verwendet die in .env hinterlegten API-Credentials, fragt interaktiv nach dem
Login-Code (per Telegram-App oder SMS zugestellt) und ggf. dem 2FA-Passwort.
Beim Erfolg legt Telethon eine SQLite-Session-Datei 'telegram.session' im
Projektordner an. Diese Datei muss erhalten bleiben, damit das Sync-Skript ohne
weitere Interaktion senden kann.

Aufruf:
    .venv/bin/python init_telegram.py
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
except ImportError:
    sys.exit("Telethon nicht installiert. Zuerst 'bash setup_venv.sh' laufen lassen "
             "und dieses Skript dann mit .venv/bin/python aufrufen.")

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
SESSION_PATH = SCRIPT_DIR / "telegram"  # Telethon hängt .session selbst an


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


async def run() -> None:
    env = load_env(ENV_PATH)
    api_id = int(require(env, "TELEGRAM_API_ID"))
    api_hash = require(env, "TELEGRAM_API_HASH")
    phone = require(env, "TELEGRAM_PHONE")
    target = require(env, "TELEGRAM_TARGET")

    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        code = input("Telegram-Login-Code (aus der Telegram-App oder SMS): ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = getpass.getpass("2FA-Passwort: ")
            await client.sign_in(password=password)

    me = await client.get_me()
    print(f"Eingeloggt als: {me.first_name} (@{me.username}), id={me.id}")

    # Ziel-Auflösung als Probe — schlägt früh fehl, falls Username falsch.
    try:
        entity = await client.get_entity(target)
        label = getattr(entity, "username", None) or getattr(entity, "first_name", str(entity))
        print(f"Ziel erreichbar: {label} (id={entity.id})")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNUNG: Ziel '{target}' konnte nicht aufgelöst werden: {exc}")
        print("Falls der Bot existiert, einmal manuell anschreiben oder Username/ID korrigieren.")

    await client.disconnect()
    print(f"Session gespeichert: {SESSION_PATH}.session")
    print("Setup abgeschlossen — withings_to_telegram.py kann jetzt laufen.")


if __name__ == "__main__":
    asyncio.run(run())
