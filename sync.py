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


def get_players_data():
    """
    Devuelve (id_a_nombre, id_a_valor_mercado) de todos los jugadores de
    La Liga. Prueba varias rutas porque una de ellas puede estar
    bloqueada para tráfico automatizado (protección anti-bots); si todas
    fallan, devolvemos diccionarios vacíos y seguimos sin nombres/valores
    (mejor eso que romper la sincronización).
    """
    attempts = [
        "https://biwenger.as.com/api/v2/competitions/la-liga/data",
        "https://cf.biwenger.com/api/v2/competitions/la-liga/data",
    ]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
    for url in attempts:
        try:
            r = requests.get(url, params={"score": 2}, headers=headers, timeout=20)
            print(f"[debug] Probando listado de jugadores en {url} -> status {r.status_code}")
            if not r.ok:
                continue
            data = r.json()
            data = data.get("data", data)
            players_data = data.get("players", {})

            entries = []
            if isinstance(players_data, dict):
                entries = [(pid, p) for pid, p in players_data.items()]
            elif isinstance(players_data, list):
                entries = [(p.get("id"), p) for p in players_data if isinstance(p, dict)]

            names, values = {}, {}
            for pid, p in entries:
                if pid is None or not isinstance(p, dict):
                    continue
                if p.get("name"):
                    names[str(pid)] = p["name"]
                pv = p.get("price")
                if pv is None:
                    pv = p.get("marketValue")
                if pv is None:
                    pv = p.get("value")
                if isinstance(pv, (int, float)):
                    values[str(pid)] = pv

            if names:
                print(f"Nombres de jugadores descargados: {len(names)} (con valor de mercado: {len(values)})")
                return names, values
        except Exception as e:
            print(f"[aviso] Fallo consultando {url}: {e}")

    print("[aviso] No se pudo descargar el listado de jugadores por ninguna vía. Se usarán números de ficha.")
    return {}, {}


def fetch_full_board(token, league_id, league_user_id, page_size=500, max_pages=20):
    """
    Trae el tablón de actividad paginando, en vez de fiarnos de un único
    "limit" fijo que algún día se quede corto y nos haga perder
    movimientos antiguos. Para en cuanto una página viene más corta que
    lo pedido (fin de los datos) o si detecta que la paginación no
    avanza (por si Biwenger no soporta "offset" como esperamos, en cuyo
    caso lo veremos en el log y lo ajustamos).
    """
    all_events = []
    seen_first_marker = None
    offset = 0
    for page in range(max_pages):
        page_events = get_json(
            f"{API}/league/{league_id}/board", token, league_id, league_user_id,
            params={"limit": page_size, "offset": offset}
        )
        page_events = page_events if isinstance(page_events, list) else page_events.get("data", [])
        if not page_events:
            print(f"[debug] Página {page + 1}: vacía, fin del tablón.")
            break
        marker = (page_events[0].get("id"), page_events[0].get("date"))
        print(f"[debug] Página {page + 1}: {len(page_events)} eventos (offset={offset}), primer evento fecha={page_events[0].get('date')}")
        if marker == seen_first_marker:
            print("[aviso] La paginación no avanza (puede que 'offset' no esté soportado). Me quedo con lo ya traído.")
            break
        seen_first_marker = marker
        all_events.extend(page_events)
        if len(page_events) < page_size:
            break
        offset += page_size
    return all_events


def extract_transactions(board, id_to_name, players_map, players_values):
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

            amount = content.get("amount") or content.get("price")
            raw_to = content.get("to")
            to_user = raw_to if isinstance(raw_to, dict) else {}
            raw_from = content.get("from")
            from_user = raw_from if isinstance(raw_from, dict) else {}
            raw_clause_user = content.get("user")
            clause_user = raw_clause_user if isinstance(raw_clause_user, dict) else {}
            player_field = content.get("player")

            if isinstance(player_field, dict):
                player_id = player_field.get("id")
                player_name = player_field.get("name", "")
            else:
                player_id = player_field
                player_name = players_map.get(str(player_field), f"Jugador #{player_field}") if player_field is not None else ""

            # El identificador para no duplicar se construye SOLO con datos
            # estables (nunca con campos decorativos como iconos, que pueden
            # cambiar de una consulta a otra y hacer que el mismo movimiento
            # parezca "nuevo" cada vez).
            manager_id_for_key = to_user.get("id") or from_user.get("id") or clause_user.get("id")
            raw_id = ev.get("id")
            stable_key = f"{etype}|{date}|{idx}|{manager_id_for_key}|{player_id}|{amount}"
            source_id = f"{raw_id}:{idx}" if raw_id is not None else stable_key

            if etype == "clauseincrement" and amount:
                manager = clause_user.get("name") or id_to_name.get(clause_user.get("id"), "")
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
                overpay = None
                overpay_ref = None
                market_value = players_values.get(str(player_id)) if player_id is not None else None
                if isinstance(market_value, (int, float)):
                    overpay = amount - market_value
                    overpay_ref = "su valor de mercado"
                else:
                    raw_bids = content.get("bids")
                    if isinstance(raw_bids, list) and raw_bids:
                        bid_amounts = [b.get("amount") for b in raw_bids if isinstance(b, dict) and isinstance(b.get("amount"), (int, float))]
                        if bid_amounts:
                            overpay = amount - max(bid_amounts)
                            overpay_ref = "la siguiente puja"
                if buyer:
                    tx = {"manager": buyer, "type": "compra", "amount": amount, "detail": player_name, "date": date_str, "sourceId": source_id + ":buy"}
                    if overpay is not None:
                        tx["overpay"] = overpay
                        tx["overpayRef"] = overpay_ref
                    transactions.append(tx)
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

    print("Descargando tablón de actividad (con paginación completa)...")
    board_events = fetch_full_board(token, league_id, league_user_id)
    print(f"[debug] Eventos totales recibidos del tablón: {len(board_events)}")

    players_map, players_values = get_players_data()
    new_transactions = extract_transactions(board_events, id_to_name, players_map, players_values)

    own_manager_name = id_to_name.get(league_user_id)
    if own_manager_name:
        sign_map = {"compra": -1, "venta": 1, "clausula_pagada": -1, "clausula_cobrada": 1, "clausula_subida": -1, "ajuste": 1}
        own_moves = [t for t in new_transactions if t.get("manager") == own_manager_name]
        net = sum(sign_map.get(t["type"], 0) * t["amount"] for t in own_moves)
        print(f"[debug] En este lote de {len(board_events)} eventos, movimientos detectados para {own_manager_name}: {len(own_moves)} (neto: {net})")

    with open("board_raw.json", "w", encoding="utf-8") as f:
        json.dump(board_events, f, ensure_ascii=False, indent=2)

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

    # Limpieza de seguridad: si por lo que sea se coló algún duplicado
    # (identificador distinto pero mismo movimiento real), lo quitamos.
    # Cuando hay dos versiones del mismo movimiento, nos quedamos con la
    # más completa (con nombre real del jugador y sobrepago si lo tiene),
    # no con la primera que encontremos.
    def richness(t):
        score = 0
        detail = t.get("detail") or ""
        if detail and not detail.startswith("Jugador #"):
            score += 2
        if "overpay" in t:
            score += 1
        return score

    best_by_key = {}
    order = []
    for t in existing["transactions"]:
        key = (t.get("manager"), t.get("type"), t.get("amount"), t.get("date"))
        if key not in best_by_key:
            best_by_key[key] = t
            order.append(key)
        elif richness(t) > richness(best_by_key[key]):
            best_by_key[key] = t
    removed = len(existing["transactions"]) - len(order)
    existing["transactions"] = [best_by_key[k] for k in order]
    if removed:
        print(f"Movimientos duplicados eliminados: {removed}")

    existing["lastSync"] = datetime.now(timezone.utc).isoformat()
    existing["teamValues"] = team_values

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"Movimientos nuevos añadidos: {added}")
    print(f"Total movimientos guardados: {len(existing['transactions'])}")

    if own_manager_name:
        sign_map = {"compra": -1, "venta": 1, "clausula_pagada": -1, "clausula_cobrada": 1, "clausula_subida": -1, "ajuste": 1}
        all_own = [t for t in existing["transactions"] if t.get("manager") == own_manager_name]
        net_total = sum(sign_map.get(t["type"], 0) * t["amount"] for t in all_own)
        print(f"[debug] Historial completo guardado para {own_manager_name}: {len(all_own)} movimientos, neto acumulado: {net_total}")
        print(f"[debug] Compara: tu saldo real de Biwenger debería ser aprox. 40.000.000 - (tu valor de plantilla inicial en overrides.json) + {net_total}")


if __name__ == "__main__":
    main()
