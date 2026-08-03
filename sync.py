#!/usr/bin/env python3
"""
Sincroniza el tablón con Biwenger. Versión pensada para correr sola en
GitHub Actions (sin pedir nada por teclado): lee las credenciales de
variables de entorno (que en Actions vienen de los "Secrets" del repo,
cifrados) y actualiza data.json con los movimientos nuevos del mercado.

No toca overrides.json — ahí es donde vive el valor de plantilla
inicial de cada manager, y eso se edita a mano en GitHub, no aquí.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

API = "https://biwenger.as.com/api/v2"
DATA_FILE = "data.json"
OVERRIDES_FILE = "overrides.json"


def login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=15)
    if not r.ok:
        print(f"[Error {r.status_code}] al iniciar sesión: {r.text[:500]}")
        r.raise_for_status()
    return r.json()["token"]


def get_json(url, token, league_id=None, user_id=None, params=None):
    headers = {"Authorization": f"Bearer {token}"}
    if league_id is not None:
        headers["X-League"] = str(league_id)
    if user_id is not None:
        headers["X-User"] = str(user_id)
    r = requests.get(url, headers=headers, params=params, timeout=20)
    if not r.ok:
        print(f"[Error {r.status_code}] al pedir {url}: {r.text[:500]}")
        r.raise_for_status()
    data = r.json()
    return data.get("data", data)


def extract_transactions(board, id_to_name):
    events = board if isinstance(board, list) else board.get("data", [])
    transactions = []

    for ev in events:
        etype = str(ev.get("type", "")).lower()
        raw_content = ev.get("content", ev)
        content_items = raw_content if isinstance(raw_content, list) else [raw_content]
        date = ev.get("date")
        date_str = (
            datetime.fromtimestamp(date, tz=timezone.utc).strftime("%Y-%m-%d")
            if isinstance(date, (int, float))
            else str(date)[:10] if date else ""
        )

        for idx, content in enumerate(content_items):
            if not isinstance(content, dict):
                continue
            raw_id = ev.get("id")
            fallback_key = f"{etype}|{date}|{idx}|{json.dumps(content, sort_keys=True, ensure_ascii=False)}"
            source_id = f"{raw_id}:{idx}" if raw_id is not None else fallback_key

            amount = content.get("amount") or content.get("price")
            to_user = content.get("to") or {}
            from_user = content.get("from") or {}
            player_field = content.get("player")
            if isinstance(player_field, dict):
                player_name = player_field.get("name", "")
            elif player_field is not None:
                player_name = f"Jugador #{player_field}"
            else:
                player_name = ""

            if etype == "clauseincrement" and amount:
                user = content.get("user") or {}
                manager = user.get("name") or id_to_name.get(user.get("id"), "")
                if manager:
                    transactions.append({"manager": manager, "type": "clausula_subida", "amount": amount, "detail": player_name, "date": date_str, "sourceId": source_id})
            elif "clause" in etype and amount:
                buyer = to_user.get("name") or id_to_name.get(to_user.get("id"), "")
                seller = from_user.get("name") or id_to_name.get(from_user.get("id"), "")
                if buyer:
                    transactions.append({"manager": buyer, "type": "clausula_pagada", "amount": amount, "detail": player_name, "date": date_str, "sourceId": source_id + ":buy"})
                if seller:
                    transactions.append({"manager": seller, "type": "clausula_cobrada", "amount": amount, "detail": player_name, "date": date_str, "sourceId": source_id + ":sell"})
            elif ("transfer" in etype or "market" in etype) and amount:
                buyer = to_user.get("name") or id_to_name.get(to_user.get("id"), "")
                seller = from_user.get("name") or id_to_name.get(from_user.get("id"), "")
                if buyer:
                    transactions.append({"manager": buyer, "type": "compra", "amount": amount, "detail": player_name, "date": date_str, "sourceId": source_id + ":buy"})
                if seller:
                    transactions.append({"manager": seller, "type": "venta", "amount": amount, "detail": player_name, "date": date_str, "sourceId": source_id + ":sell"})

    return transactions


def main():
    email = os.environ.get("BIWENGER_EMAIL")
    password = os.environ.get("BIWENGER_PASSWORD")
    league_id = os.environ.get("BIWENGER_LEAGUE_ID")

    if not email or not password or not league_id:
        sys.exit("Faltan variables de entorno: BIWENGER_EMAIL, BIWENGER_PASSWORD, BIWENGER_LEAGUE_ID")

    print("Iniciando sesión...")
    token = login(email, password)

    r = requests.get(f"{API}/account", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    r.raise_for_status()
    account = r.json()
    account = account.get("data", account)
    account_user_id = (account.get("account") or {}).get("id") or account.get("id")

    league = next((lg for lg in account.get("leagues", []) if str(lg.get("id")) == str(league_id)), None)
    if not league:
        sys.exit(f"No encontré la liga {league_id} en esta cuenta.")
    league_user_id = (league.get("user") or {}).get("id") or account_user_id

    print("Descargando clasificación (para nombres y valor de equipo)...")
    standings = get_json(
        f"{API}/league/{league_id}", token, league_id, league_user_id,
        params={"include": "all", "fields": "*,standings"}
    )
    standings_list = standings.get("standings", [])
    id_to_name = {s.get("id"): s.get("name") for s in standings_list if s.get("id") is not None}
    team_values = {s.get("name"): s.get("teamValue") for s in standings_list if s.get("name") and s.get("teamValue") is not None}

    print("Descargando tablón de actividad...")
    board = get_json(f"{API}/league/{league_id}/board", token, league_id, league_user_id, params={"limit": 300})
    new_transactions = extract_transactions(board, id_to_name)

    with open("board_raw.json", "w", encoding="utf-8") as f:
        json.dump(board, f, ensure_ascii=False, indent=2)

    # Cargar lo que ya teníamos y añadir solo lo nuevo (sin duplicar)
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {"transactions": []}

    existing_ids = {t.get("sourceId") for t in existing.get("transactions", []) if t.get("sourceId")}
    added = 0
    for t in new_transactions:
        if t.get("sourceId") in existing_ids:
            continue
        existing["transactions"].append(t)
        existing_ids.add(t.get("sourceId"))
        added += 1

    existing["lastSync"] = datetime.now(timezone.utc).isoformat()
    existing["teamValues"] = team_values

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"Movimientos nuevos añadidos: {added}")
    print(f"Total movimientos guardados: {len(existing['transactions'])}")


if __name__ == "__main__":
    main()
