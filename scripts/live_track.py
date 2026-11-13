#!/usr/bin/env python3
"""Seguimiento en vivo de XA-UWK.

Consulta la posicion en vivo cada INTERVAL s (api.adsb.lol; respaldo
opendata.adsb.fi) y publica live.json en la rama 'live' del repo (un solo
commit que se reemplaza, sin historial). La app lo lee cada ~10 s.

Termina cuando el avion lleva LINGER_MIN en tierra despues de aterrizar
(espera por si hay escala), si deja de verse 30 min, o a los MAX_MINUTES.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adsb_history as H  # noqa: E402

INTERVAL = int(os.environ.get("INTERVAL") or 10)
PUSH_EVERY = int(os.environ.get("PUSH_EVERY") or 20)
MAX_MIN = int(os.environ.get("MAX_MINUTES") or 300)
LINGER_MIN = int(os.environ.get("LINGER_MIN") or 75)
LOST_MIN = 30
WAIT_MIN = int(os.environ.get("WAIT_MIN") or 60)  # espera inicial si el avion no se ve
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GH_TOKEN", "")
WORK = "/tmp/livebranch"
MTY = timezone(timedelta(hours=-6))
SOURCES = [f"https://api.adsb.lol/v2/hex/{H.ICAO}",
           f"https://opendata.adsb.fi/api/v2/hex/{H.ICAO}"]


def mty_day(t):
    return datetime.fromtimestamp(t, MTY).strftime("%Y-%m-%d")


def poll():
    """Devuelve (posicion o None, fuente)."""
    for url in SOURCES:
        try:
            with H.http(url, timeout=15) as r:
                d = json.load(r)
        except Exception as e:  # noqa: BLE001
            print(f"poll {url.split('/')[2]}: {e}")
            continue
        ac = (d.get("ac") or [None])[0]
        src = url.split("/")[2]
        if not ac or ac.get("lat") is None:
            return None, src
        now = time.time()
        alt = ac.get("alt_baro")
        ground = alt == "ground"
        gs = ac.get("gs")
        if isinstance(gs, (int, float)) and gs < 35:
            ground = True
        return {
            "t": int(now - float(ac.get("seen_pos") or ac.get("seen") or 0)),
            "lat": round(ac["lat"], 5), "lon": round(ac["lon"], 5),
            "alt": None if ground or not isinstance(alt, (int, float)) else int(alt),
            "gs": round(gs) if isinstance(gs, (int, float)) else None,
            "trk": round(ac.get("track") if ac.get("track") is not None else (ac.get("true_heading") or 0)),
            "vr": ac.get("baro_rate") if ac.get("baro_rate") is not None else ac.get("geom_rate"),
            "sq": ac.get("squawk"), "ground": ground,
        }, src
    return None, None


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=WORK, check=check, capture_output=True, text=True)


def setup_branch():
    os.makedirs(WORK, exist_ok=True)
    git("init", "-q")
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("remote", "add", "origin", f"https://x-access-token:{TOKEN}@github.com/{REPO}.git", check=False)
    prev = {}
    if git("fetch", "-q", "--depth=1", "origin", "live", check=False).returncode == 0:
        r = git("show", "FETCH_HEAD:live.json", check=False)
        if r.returncode == 0:
            try:
                prev = json.loads(r.stdout)
            except json.JSONDecodeError:
                prev = {}
    git("checkout", "-q", "--orphan", "live", check=False)
    return prev


_first = [True]


def publish(obj):
    with open(os.path.join(WORK, "live.json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
    git("add", "live.json")
    msg = f"live {datetime.now(MTY).strftime('%Y-%m-%d %H:%M:%S')}"
    if _first[0]:
        git("commit", "-q", "-m", msg)
        _first[0] = False
    else:
        git("commit", "-q", "--amend", "-m", msg)
    r = git("push", "-q", "-f", "origin", "live", check=False)
    if r.returncode != 0:
        print("push:", r.stderr.strip()[:200])


def ap_info(a):
    return {"code": a["code"], "name": a["name"], "city": a["city"], "lat": a["lat"], "lon": a["lon"]}


def near_info(aps, p):
    cands = list(aps.near(p["lat"], p["lon"], 150))
    big = [x for x in cands if x[0]["type"] in ("large_airport", "medium_airport")]
    best = min(big or cands, key=lambda x: x[1], default=None)
    if not best:
        return None
    a, d = best
    b = H.bearing(a["lat"], a["lon"], p["lat"], p["lon"])
    card = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"][int((b + 22.5) // 45) % 8]
    return {**ap_info(a), "d": round(d, 1), "dir": card}


def main():
    if not REPO or not TOKEN:
        print("Falta GITHUB_REPOSITORY / GH_TOKEN")
        return 1
    aps = H.Airports()
    prev = setup_branch()
    start = time.time()
    today = mty_day(start)
    flights_today = prev.get("today", []) if prev.get("date") == today else []
    cur = prev.get("flight") if prev.get("date") == today and prev.get("flight") else None
    last_ground = None
    last_seen = time.time()
    ground_since = None
    pos, src = None, None
    last_push = 0
    used = {}

    def remember(a):
        if a:
            used[a["code"]] = ap_info(a)
        return a["code"] if a else None

    for f in flights_today + ([cur] if cur else []):
        for c in (f.get("dep"), f.get("arr")):
            if c and c in aps.by_code:
                remember(aps.by_code[c])

    while True:
        now = time.time()
        p, s = poll()
        if s:
            src = s
        if p:
            pos, last_seen = p, now
            if p["ground"]:
                if cur:  # aterrizo
                    a = aps.at_ground(p["lat"], p["lon"])
                    cur.update(arr=remember(a), on=p["t"])
                    flights_today.append(cur)
                    cur = None
                if ground_since is None:
                    ground_since = now
                last_ground = p
            else:
                ground_since = None
                if cur is None:
                    dep = None
                    if last_ground and p["t"] - last_ground["t"] < 1200:
                        dep = aps.at_ground(last_ground["lat"], last_ground["lon"])
                    else:
                        dep = H.low_airport((p["t"], p["lat"], p["lon"], p["alt"], p["gs"], p["trk"], p["vr"]), aps)
                    cur = {"dep": remember(dep), "off": p["t"] if dep else None,
                           "partial": dep is None, "track": []}
                tr = cur["track"]
                if not tr or tr[-1][0] != p["t"]:
                    tr.append([p["t"], p["lat"], p["lon"], p["alt"]])
        # condiciones para terminar
        done = None
        if now - start > MAX_MIN * 60:
            done = "tiempo maximo"
        elif now - last_seen > LOST_MIN * 60:
            done = "sin senal"
            if cur:
                cur.update(arr=None, lost=True)
                flights_today.append(cur)
                cur = None
        elif ground_since and now - ground_since > LINGER_MIN * 60:
            done = "en tierra"
        elif pos is None and now - start > WAIT_MIN * 60:
            done = "sin datos"
        tracking = done is None
        if now - last_push >= PUSH_EVERY or not tracking:
            near = near_info(aps, pos) if pos else None
            if near:
                used[near["code"]] = {k: near[k] for k in ("code", "name", "city", "lat", "lon")}
            publish({
                "updated": int(now), "tracking": tracking, "end": done, "src": src,
                "seen": int(last_seen) if pos else None,
                "started": int(start), "date": today,
                "pos": pos, "near": near, "flight": cur, "today": flights_today[-12:],
                "airports": used,
            })
            last_push = now
        if not tracking:
            print("Fin del seguimiento:", done)
            return 0
        time.sleep(INTERVAL if (pos and not pos["ground"]) else 30)


if __name__ == "__main__":
    sys.exit(main())
