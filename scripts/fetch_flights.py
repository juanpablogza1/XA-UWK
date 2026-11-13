#!/usr/bin/env python3
"""Descarga el historial de vuelos de XA-UWK (ICAO24 0d0929) desde OpenSky
y lo acumula en data/flights.json. Pensado para correr en GitHub Actions.

Cada corrida:
  1. Consulta los ultimos RECENT_DAYS dias (vuelos nuevos).
  2. Sigue cargando el historial hacia atras hasta cubrir HISTORY_DAYS,
     con un maximo de MAX_BACKFILL consultas por corrida. El avance se
     guarda en data/meta.json, asi que el año completo se completa en
     varias corridas sin pasarse del limite diario de creditos.

OpenSky cobra 30 creditos por consulta de 1-2 dias y limita los creditos
por dia; si responde 429 el script se detiene y continua en la siguiente
corrida.

Variables de entorno:
  OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET  credenciales OAuth2 de OpenSky
  HISTORY_DAYS   dias de historial a cubrir (default 365)
  RECENT_DAYS    dias recientes a re-consultar en cada corrida (default 3)
  MAX_BACKFILL   consultas de historial por corrida (default 25)
"""
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ICAO24 = "0d0929"
API = "https://opensky-network.org/api"
TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network/"
             "protocol/openid-connect/token")
AIRPORTS_CSV = ("https://raw.githubusercontent.com/davidmegginson/"
                "ourairports-data/main/airports.csv")
DAY = 86400

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
F_FLIGHTS = os.path.join(DATA, "flights.json")
F_AIRPORTS = os.path.join(DATA, "airports.json")
F_LIVE = os.path.join(DATA, "live.json")
F_META = os.path.join(DATA, "meta.json")


class RateLimited(Exception):
    pass


def env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def iso_day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def get_token():
    cid = os.environ.get("OPENSKY_CLIENT_ID")
    sec = os.environ.get("OPENSKY_CLIENT_SECRET")
    if not cid or not sec:
        print("Sin credenciales OpenSky: consulta anonima (puede fallar).")
        return None
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": sec,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def api_get(path, params, token):
    """Devuelve el JSON, [] si no hay datos, None si hay error.
    Lanza RateLimited si se acabaron los creditos del dia."""
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for _ in range(3):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # sin vuelos en el intervalo
                return []
            if e.code == 429:
                wait = int(e.headers.get("X-Rate-Limit-Retry-After-Seconds", 60))
                if wait > 120:
                    print(f"Creditos agotados (reintentar en {wait // 60} min)")
                    raise RateLimited()
                print(f"Limite momentaneo, esperando {wait}s")
                time.sleep(wait)
                continue
            print(f"HTTP {e.code} en {path}: {e.read()[:200]!r}")
            return None
        except urllib.error.URLError as e:
            print(f"Error de red: {e}")
            time.sleep(5)
    return None


def query_day(d, now, token, flights):
    """Ventana de 2 dias UTC [d-1, d]: maximo permitido (2 particiones) y
    con traslape para no perder vuelos que cruzan la medianoche UTC."""
    res = api_get("/flights/aircraft", {
        "icao24": ICAO24,
        "begin": d - DAY,
        "end": min(now, d + DAY - 1),
    }, token)
    if res is None:
        return False
    for f in res:
        flights[str(f["firstSeen"])] = {
            "firstSeen": f["firstSeen"],
            "lastSeen": f["lastSeen"],
            "dep": f.get("estDepartureAirport"),
            "arr": f.get("estArrivalAirport"),
            "callsign": (f.get("callsign") or "").strip(),
        }
    return True


def enrich_airports(codes):
    known = load(F_AIRPORTS, {})
    missing = {c for c in codes if c and c not in known}
    if not missing:
        return known
    print(f"Buscando {len(missing)} aeropuertos en OurAirports")
    with urllib.request.urlopen(AIRPORTS_CSV, timeout=120) as r:
        text = r.read().decode("utf-8")
    for row in csv.DictReader(io.StringIO(text)):
        for c in (row.get("ident"), row.get("gps_code"), row.get("icao_code")):
            if c in missing:
                known[c] = {
                    "name": row["name"],
                    "city": row.get("municipality") or "",
                    "country": row.get("iso_country") or "",
                    "iata": row.get("iata_code") or "",
                    "lat": float(row["latitude_deg"]),
                    "lon": float(row["longitude_deg"]),
                }
                missing.discard(c)
                break
    for c in missing:
        known[c] = {"name": c, "city": "", "country": "", "iata": "",
                    "lat": None, "lon": None}
    return known


def main():
    history_days = env_int("HISTORY_DAYS", 365)
    recent_days = env_int("RECENT_DAYS", 3)
    max_backfill = env_int("MAX_BACKFILL", 25)

    now = int(time.time())
    today = now - now % DAY
    target = today - (history_days - 1) * DAY

    meta = load(F_META, {})
    flights = {str(f["firstSeen"]): f for f in load(F_FLIGHTS, [])}
    before = len(flights)

    # Dia mas antiguo ya consultado de forma continua hacia atras.
    oldest = meta.get("oldest_day")
    if oldest is None and meta.get("days_checked") and meta.get("updated"):
        up = meta["updated"]
        oldest = up - up % DAY - (meta["days_checked"] - 1) * DAY
    if oldest is None:
        oldest = today + DAY  # nada consultado aun
    newest = meta.get("newest_day")
    if newest is None and meta.get("updated"):
        newest = meta["updated"] - meta["updated"] % DAY
    # Si no hubo corridas por varios dias, cubrir el hueco (max 30 dias).
    span = recent_days
    if newest is not None:
        span = max(span, min(30, (today - newest) // DAY + recent_days))

    token = get_token()
    errors = 0
    used = 0
    limited = False
    try:
        # 1) Dias recientes (OpenSky publica los vuelos al dia siguiente)
        for i in range(span):
            d = today - i * DAY
            ok = query_day(d, now, token, flights)
            used += 1
            errors += not ok
            oldest = min(oldest, d)
            newest = today
            time.sleep(1)
        # 2) Historial hacia atras
        n = 0
        while oldest > target and n < max_backfill:
            d = oldest - DAY
            ok = query_day(d, now, token, flights)
            used += 1
            n += 1
            errors += not ok
            oldest = d  # avanza aun si falla para no atorarse en un dia malo
            time.sleep(1)
    except RateLimited:
        limited = True

    ordered = sorted(flights.values(), key=lambda f: f["firstSeen"], reverse=True)
    save(F_FLIGHTS, ordered)
    codes = {f["dep"] for f in ordered} | {f["arr"] for f in ordered}
    save(F_AIRPORTS, enrich_airports(codes))

    state = None
    if not limited:
        try:
            live = api_get("/states/all", {"icao24": ICAO24}, token)
        except RateLimited:
            live = None
        if live and live.get("states"):
            s = live["states"][0]
            state = {"time": live["time"], "callsign": (s[1] or "").strip(),
                     "lon": s[5], "lat": s[6],
                     "alt_m": s[7] if s[7] is not None else s[13],
                     "on_ground": s[8], "speed_ms": s[9], "track": s[10]}
    save(F_LIVE, {"checked": now, "state": state})

    covered_from = max(oldest - DAY, target - DAY) if oldest <= today else None
    save(F_META, {
        "updated": now,
        "updated_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "oldest_day": oldest,
        "newest_day": newest,
        "history_from": iso_day(covered_from) if covered_from else None,
        "history_target_days": history_days,
        "history_complete": oldest <= target,
        "queries": used,
        "errors": errors,
        "rate_limited": limited,
    })
    print(f"Vuelos: {before} -> {len(ordered)} | consultas: {used} | "
          f"errores: {errors} | historial desde "
          f"{iso_day(covered_from) if covered_from else '-'} | "
          f"completo: {oldest <= target}")
    if errors and errors == used and not ordered:
        sys.exit(1)


if __name__ == "__main__":
    main()
