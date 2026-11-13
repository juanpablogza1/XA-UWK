#!/usr/bin/env python3
"""Descarga el historial de vuelos de XA-UWK (ICAO24 0d0929) desde OpenSky
y lo acumula en data/flights.json. Pensado para correr en GitHub Actions.

Variables de entorno:
  OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET  credenciales OAuth2 de OpenSky
  BACKFILL_DAYS                              dias hacia atras a consultar (default 4)
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
F_FLIGHTS = os.path.join(DATA, "flights.json")
F_AIRPORTS = os.path.join(DATA, "airports.json")
F_LIVE = os.path.join(DATA, "live.json")
F_META = os.path.join(DATA, "meta.json")


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
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # sin vuelos en el intervalo
                return []
            if e.code == 429:
                wait = int(e.headers.get("X-Rate-Limit-Retry-After-Seconds", 60))
                print(f"Limite de creditos, esperando {wait}s")
                time.sleep(min(wait, 120))
                continue
            print(f"HTTP {e.code} en {path}: {e.read()[:200]!r}")
            return None
        except urllib.error.URLError as e:
            print(f"Error de red: {e}")
            time.sleep(5)
    return None


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
    days = int(os.environ.get("BACKFILL_DAYS") or 4)
    token = get_token()
    now = int(time.time())
    flights = {str(f["firstSeen"]): f for f in load(F_FLIGHTS, [])}
    before = len(flights)
    errors = 0

    # OpenSky particiona por dia UTC y solo permite 2 particiones por consulta.
    # Ventanas de 2 dias alineadas a medianoche UTC, avanzando 1 dia, para
    # que los vuelos que cruzan la medianoche UTC tambien se capturen.
    today = now - now % 86400
    for i in range(days):
        day = today - i * 86400
        begin = day - 86400
        end = min(now, day + 86400 - 1)
        res = api_get("/flights/aircraft",
                      {"icao24": ICAO24, "begin": begin, "end": end}, token)
        if res is None:
            errors += 1
        else:
            for f in res:
                flights[str(f["firstSeen"])] = {
                    "firstSeen": f["firstSeen"],
                    "lastSeen": f["lastSeen"],
                    "dep": f.get("estDepartureAirport"),
                    "arr": f.get("estArrivalAirport"),
                    "callsign": (f.get("callsign") or "").strip(),
                }
        time.sleep(1)

    ordered = sorted(flights.values(), key=lambda f: f["firstSeen"], reverse=True)
    save(F_FLIGHTS, ordered)
    codes = {f["dep"] for f in ordered} | {f["arr"] for f in ordered}
    save(F_AIRPORTS, enrich_airports(codes))

    live = api_get("/states/all", {"icao24": ICAO24}, token)
    state = None
    if live and live.get("states"):
        s = live["states"][0]
        state = {"time": live["time"], "callsign": (s[1] or "").strip(),
                 "lon": s[5], "lat": s[6], "alt_m": s[7] if s[7] is not None else s[13],
                 "on_ground": s[8], "speed_ms": s[9], "track": s[10]}
    save(F_LIVE, {"checked": now, "state": state})

    save(F_META, {"updated": now,
                  "updated_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                  "days_checked": days, "errors": errors})
    print(f"Vuelos: {before} -> {len(ordered)} (errores: {errors})")
    if errors and len(ordered) == before == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
