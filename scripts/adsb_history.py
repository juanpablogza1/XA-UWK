#!/usr/bin/env python3
"""Historial de vuelos de XA-UWK (Pilatus PC-12, Mode S 0D0929).

Fuente: historial abierto de adsb.lol (github.com/adsblol, licencia ODbL 1.0).
Cada dia de adsb.lol es un .tar de ~3.5 GB con un archivo por avion. Con
peticiones HTTP Range en paralelo se ubica el directorio del avion y se leen
solo unas decenas de MB por dia.

Pasos de cada corrida:
  1. Descarga las trazas de los dias que faltan (hasta HISTORY_DAYS atras)
     y las guarda en data/raw/AAAA-MM-DD.json.gz.
  2. Reconstruye todos los vuelos con las trazas guardadas.
  3. Origen/destino de cada vuelo:
       tierra      el avion se vio en tierra en el aeropuerto (observado)
       escala      se perdio la senal y reaparecio despegando del mismo lugar;
                   el aeropuerto se estima con ambas trayectorias
       secuencia   coincide con el aeropuerto del vuelo anterior/siguiente
       trayectoria estimado con rumbo, altitud y distancia al perder la senal
  4. Escribe data/flights.json, airports.json, meta.json y live.json.
"""
import csv
import gzip
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

ICAO = "0d0929"
SUBDIR = ICAO[-2:]
TARGET = f"trace_full_{ICAO}.json"
TAGS = ("prod-0", "staging-0")
UA = {"User-Agent": "xa-uwk-history/2.0 (+https://github.com/juanpablogza1/XA-UWK)"}
AIRPORTS_CSV = ("https://raw.githubusercontent.com/davidmegginson/"
                "ourairports-data/main/airports.csv")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RAW = os.path.join(DATA, "raw")
F_DAYS = os.path.join(DATA, "days.json")
F_FLIGHTS = os.path.join(DATA, "flights.json")
F_AIRPORTS = os.path.join(DATA, "airports.json")
F_META = os.path.join(DATA, "meta.json")
F_LIVE = os.path.join(DATA, "live.json")

HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS") or 366)
TIME_BUDGET = int(os.environ.get("TIME_BUDGET") or 2400)
MB = 1 << 20


# --------------------------------------------------------------------- util
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj, compact=False):
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def save_if_changed(path, obj, ignore=()):
    """Guarda solo si cambio algo ademas de las marcas de tiempo (evita commits vacios)."""
    old = load(path, None)
    strip = (lambda o: {k: v for k, v in o.items() if k not in ignore}) if isinstance(obj, dict) else (lambda o: o)
    if isinstance(old, type(obj)) and strip(old) == strip(obj):
        return False
    save(path, obj)
    return True


def http(url, headers=None, timeout=60):
    h = dict(UA)
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)


def retry(fn, tries=4):
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in (404, 416) or i == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if i == tries - 1:
                raise
        time.sleep(2 * (i + 1))


# ------------------------------------------------------- adsb.lol releases
class Release:
    """Un dia del historial de adsb.lol (tar partido en .aa, .ab, ...)."""

    def __init__(self, day, tag):
        self.day, self.tag = day, tag
        self.parts, self.total = [], 0

    def open(self):
        name = f"v{self.day.replace('-', '.')}-planes-readsb-{self.tag}"
        base = (f"https://github.com/adsblol/globe_history_{self.day[:4]}"
                f"/releases/download/{name}/{name}.tar")
        with ThreadPoolExecutor(4) as ex:
            infos = list(ex.map(self._probe, [base + s for s in (".aa", ".ab", ".ac", ".ad")]))
        for info in infos:
            if not info:
                break
            self.parts.append(info)
        if not self.parts:
            info = self._probe(base)
            if info:
                self.parts.append(info)
        self.total = sum(s for _, s in self.parts)
        return bool(self.parts)

    @staticmethod
    def _probe(url):
        try:
            r = retry(lambda: http(url, {"Range": "bytes=0-0"}, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        with r:
            cr = r.headers.get("Content-Range", "")
            final = r.geturl()
            r.read()
        m = re.search(r"/(\d+)$", cr)
        return (final, int(m.group(1))) if m else None

    def read(self, off, n):
        out = bytearray()
        base = 0
        for url, size in self.parts:
            if off < base + size and off + n > base:
                s = max(off, base) - base
                e = min(off + n, base + size) - base - 1

                def get(url=url, s=s, e=e):
                    with http(url, {"Range": f"bytes={s}-{e}"}) as r:
                        return r.read()
                out += retry(get)
            base += size
        return bytes(out)

    def stream(self, off=0):
        base = 0
        for url, size in self.parts:
            if off < base + size:
                s = max(off, base) - base
                r = retry(lambda url=url, s=s: http(url, {"Range": f"bytes={s}-"}, timeout=120))
                with r:
                    while True:
                        b = r.read(MB)
                        if not b:
                            break
                        yield b
            base += size


class Reader:
    def __init__(self, it):
        self.it, self.buf = it, bytearray()

    def _fill(self):
        try:
            self.buf += next(self.it)
            return True
        except StopIteration:
            return False

    def read(self, n):
        while len(self.buf) < n and self._fill():
            pass
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def skip(self, n):
        while n > 0:
            if not self.buf and not self._fill():
                return
            k = min(n, len(self.buf))
            del self.buf[:k]
            n -= k


def parse_header(blk):
    if len(blk) < 512 or blk[257:262] != b"ustar":
        return None
    chk = sum(blk[:148]) + 256 + sum(blk[156:512])
    try:
        stored = int(blk[148:156].replace(b"\0", b" ").strip() or b"0", 8)
        size = int(blk[124:136].replace(b"\0", b" ").strip() or b"0", 8)
    except ValueError:
        return None
    if chk != stored:
        return None
    name = blk[:100].split(b"\0")[0].decode("utf-8", "replace")
    if blk[257:263] == b"ustar\0":
        prefix = blk[345:500].split(b"\0")[0].decode("utf-8", "replace")
        if prefix:
            name = prefix + "/" + name
    return name, size, blk[156:157]


def dir_of(name):
    m = re.match(r"\./traces/([^/]+)/", name)
    return m.group(1) if m else None


def walk(rel, start, on_member, limit=None):
    """Recorre los miembros del tar desde 'start'. on_member(name, size, read)
    devuelve True para detenerse. read() lee el contenido del miembro."""
    rd = Reader(rel.stream(start))
    pos, longname = start, None
    while limit is None or pos - start < limit:
        blk = rd.read(512)
        if len(blk) < 512 or blk == b"\0" * 512:
            return "end"
        h = parse_header(blk)
        if h is None:
            return "desync"
        name, size, typ = h
        padded = (size + 511) // 512 * 512
        pos += 512 + padded
        if typ == b"L":
            longname = rd.read(padded)[:size].split(b"\0")[0].decode("utf-8", "replace")
            continue
        if longname:
            name, longname = longname, None
        consumed = []

        def read(padded=padded, size=size):
            consumed.append(1)
            return rd.read(padded)[:size]
        if on_member(name, size, read):
            return "stop"
        if not consumed:
            rd.skip(padded)
    return "limit"


def full_scan(rel):
    """Lee el tar completo: devuelve (traza o None, orden de directorios)."""
    order, seen, found = [], set(), []

    def cb(name, size, read):
        d = dir_of(name)
        if d and d not in seen:
            seen.add(d)
            order.append(d)
        if name.endswith("/" + TARGET):
            found.append(read())
        return False
    walk(rel, 0, cb)
    return (found[0] if found else None), order


def probe(rel, off, limit):
    off -= off % 512
    for win in (MB, 4 * MB):
        if off >= limit:
            return None
        data = rel.read(off, min(win, limit - off))
        for i in range(0, len(data) - 511, 512):
            h = parse_header(data[i:i + 512])
            if h:
                return off + i, h[0]
        off += len(data) - len(data) % 512
    return None


def probe_small(rel, off, win=128 * 1024):
    off -= off % 512
    if off >= rel.total:
        return None
    try:
        data = rel.read(off, min(win, rel.total - off))
    except Exception:  # noqa: BLE001
        return None
    for i in range(0, len(data) - 511, 512):
        h = parse_header(data[i:i + 512])
        if h:
            return off + i, h[0]
    return None


def traces_start(rel):
    """Busqueda binaria del inicio de ./traces/ (despues de ./heatmap/)."""
    lo, hi = 0, rel.total
    while hi - lo > 8 * MB:
        mid = (lo + hi) // 2
        h = probe(rel, mid, hi)
        if h is None or not h[1].startswith("./traces"):
            lo = mid if h is None else h[0] + 512
        else:
            hi = h[0]
    return lo


def locate_trace(rel, spacing=8 * MB, workers=32):
    """Ubica el archivo del avion sin leer el tar completo: sondeos en paralelo
    para encontrar el directorio traces/SUBDIR (los directorios son contiguos
    pero su orden cambia cada dia) y luego lectura secuencial de ese tramo.
    Devuelve ('found', bytes) | ('absent', None) | ('fail', None)."""
    offs = list(range(0, rel.total, spacing))
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(lambda o: probe_small(rel, o), offs))
    lab = sorted({r for r in res if r}, key=lambda r: r[0])
    for _ in range(3):
        hits = [i for i, r in enumerate(lab) if dir_of(r[1]) == SUBDIR]
        if hits:
            break
        # refinar entre sondeos consecutivos de directorios distintos
        extra = []
        for a, b in zip(lab, lab[1:]):
            if dir_of(a[1]) != dir_of(b[1]) and b[0] - a[0] > MB:
                step = max(MB, (b[0] - a[0]) // 4)
                extra += list(range(a[0] + step, b[0], step))
        if not extra:
            break
        with ThreadPoolExecutor(workers) as ex:
            more = [r for r in ex.map(lambda o: probe_small(rel, o), extra) if r]
        lab = sorted(set(lab) | set(more), key=lambda r: r[0])
    hits = [i for i, r in enumerate(lab) if dir_of(r[1]) == SUBDIR]
    if not hits:
        return "fail", None
    i = hits[0]
    start = lab[i - 1][0] if i > 0 else lab[i][0]
    state = {"seen": False, "data": None, "res": None}

    def cb(name, size, read):
        if name.endswith("/" + TARGET):
            state["data"] = read()
            return True
        d = dir_of(name)
        if d == SUBDIR:
            state["seen"] = True
        elif state["seen"]:
            # los miembros de un directorio son contiguos: ya se paso traces/SUBDIR
            state["res"] = "absent"
            return True
        return False
    walk(rel, start, cb, limit=200 * MB)
    if state["data"] is not None:
        return "found", state["data"]
    return state["res"] or "fail", None


def parse_trace(raw):
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    d = json.loads(raw)
    ts = d.get("timestamp", 0)
    pts = []
    for p in d.get("trace") or []:
        if len(p) < 6 or p[1] is None or p[2] is None:
            continue
        alt = p[3]
        if alt == "ground":
            alt = "g"
        elif isinstance(alt, (int, float)):
            alt = int(alt)
        else:
            alt = None
        gs = int(round(p[4])) if isinstance(p[4], (int, float)) else None
        trk = int(round(p[5])) % 360 if isinstance(p[5], (int, float)) else None
        vr = int(p[7]) if len(p) > 7 and isinstance(p[7], (int, float)) else None
        pts.append([int(round(ts + p[0])), round(p[1], 5), round(p[2], 5), alt, gs, trk, vr])
    info = {k: d.get(k) for k in ("r", "t", "desc", "ownOp", "year") if d.get(k)}
    return pts, info


def write_raw(day, tag, pts, info):
    os.makedirs(RAW, exist_ok=True)
    body = json.dumps({"day": day, "source": f"adsb.lol {tag}", "info": info,
                       "points": pts}, separators=(",", ":")).encode()
    with open(os.path.join(RAW, f"{day}.json.gz"), "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
            g.write(body)


def fetch_day(day):
    """Devuelve ('ok', tag, pts, info) | ('none', tag) | ('pending', None).
    adsb.lol publica copias redundantes (prod/staging): se usa la mas completa."""
    cand = [Release(day, tag) for tag in TAGS]
    with ThreadPoolExecutor(len(cand)) as ex:
        opened = list(ex.map(lambda r: r.open(), cand))
    rels = [r for r, ok in zip(cand, opened) if ok]
    if not rels:
        return "pending", None
    rel = max(rels, key=lambda r: r.total)
    res, data = locate_trace(rel)
    if res == "fail":
        print(f"   {day} {rel.tag}: sondeo sin exito, lectura completa")
        data, _ = full_scan(rel)
        res = "found" if data else "absent"
    if res == "found":
        pts, info = parse_trace(data)
        if pts:
            return "ok", rel.tag, pts, info
    return "none", rel.tag


def download(today):
    days = load(F_DAYS, {})
    t0 = time.time()
    n_new = 0
    for i in range(1, HISTORY_DAYS + 1):
        day = (today - timedelta(days=i)).isoformat()
        st = days.get(day, {}).get("status")
        if st in ("ok", "none", "missing"):
            continue
        if time.time() - t0 > TIME_BUDGET:
            print("Tiempo agotado; se continua en la siguiente corrida")
            break
        t1 = time.time()
        try:
            res = fetch_day(day)
        except Exception as e:  # noqa: BLE001
            print(f"{day}: error {e}")
            days[day] = {"status": "error", "checked": int(time.time())}
            continue
        if res[0] == "ok":
            _, tag, pts, info = res
            write_raw(day, tag, pts, info)
            days[day] = {"status": "ok", "source": tag, "points": len(pts)}
            n_new += 1
            print(f"{day}: {len(pts)} puntos ({tag}) {time.time() - t1:.1f}s")
        elif res[0] == "none":
            days[day] = {"status": "none", "source": res[1]}
            print(f"{day}: sin datos del avion {time.time() - t1:.1f}s")
        else:
            # sin release publicado: reintentar; si ya es viejo, marcar faltante
            days[day] = {"status": "missing" if i > 10 else "pending",
                         "checked": int(time.time())}
            print(f"{day}: release no publicado")
        save(F_DAYS, dict(sorted(days.items())))
    save(F_DAYS, dict(sorted(days.items())))
    return days, n_new


# ------------------------------------------------------------- geometria
R_NM = 3440.065


def dist_nm(a, b, c, d):
    p1, p2 = math.radians(a), math.radians(c)
    h = (math.sin((p2 - p1) / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2)
    return 2 * R_NM * math.asin(min(1.0, math.sqrt(h)))


def bearing(a, b, c, d):
    p1, p2, dl = math.radians(a), math.radians(c), math.radians(d - b)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def angdiff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def xtrack(o_lat, o_lon, d_lat, d_lon, p_lat, p_lon):
    """Distancia (nm) del punto P a la ruta directa O->D."""
    d13 = dist_nm(o_lat, o_lon, p_lat, p_lon) / R_NM
    t13 = math.radians(bearing(o_lat, o_lon, p_lat, p_lon))
    t12 = math.radians(bearing(o_lat, o_lon, d_lat, d_lon))
    return abs(math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(t13 - t12))))) * R_NM


def route_cost(o, d, p):
    """Penaliza si el punto observado queda lejos de la ruta directa O->D
    (los vuelos de aviacion general suelen ir casi directos)."""
    if o is None or d is None or o is d:
        return 0.0
    if dist_nm(o["lat"], o["lon"], d["lat"], d["lon"]) < 40:
        return 0.0
    # cerca de un aeropuerto dominan los procedimientos de salida/llegada
    near = min(dist_nm(o["lat"], o["lon"], p[1], p[2]), dist_nm(d["lat"], d["lon"], p[1], p[2]))
    w = max(0.0, min(1.0, (near - 20) / 60))
    if w == 0:
        return 0.0
    # peso bajo: en Mexico es comun volar por aerovias y no en linea directa
    return w * min(1.5, (xtrack(o["lat"], o["lon"], d["lat"], d["lon"], p[1], p[2]) / 25) ** 2)


def rdp(points, tol_nm):
    """Simplificacion Ramer-Douglas-Peucker (lat, lon)."""
    if len(points) < 3:
        return points
    lat0 = math.radians(points[0][0])
    xy = [(p[1] * 60 * math.cos(lat0), p[0] * 60) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        best, bi = 0.0, None
        for i in range(a + 1, b):
            px, py = xy[i]
            dd = abs(dy * (px - ax) - dx * (py - ay)) / L
            if dd > best:
                best, bi = dd, i
        if bi is not None and best > tol_nm:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(points, keep) if k]


# ------------------------------------------------------------ aeropuertos
class Airports:
    TYPES = {"large_airport", "medium_airport", "small_airport"}

    def __init__(self):
        path = "/tmp/ourairports.csv"
        if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 7 * 86400:
            with http(AIRPORTS_CSV, timeout=120) as r:
                open(path, "wb").write(r.read())
        self.grid = defaultdict(list)
        self.by_code = {}
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["type"] not in self.TYPES:
                    continue
                lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
                if not (-10 < lat < 75 and -170 < lon < -30):
                    continue
                code = (row.get("icao_code") or row.get("gps_code") or row["ident"]).strip()
                a = {
                    "code": code, "name": row["name"], "city": row.get("municipality") or "",
                    "country": row.get("iso_country") or "", "iata": row.get("iata_code") or "",
                    "lat": lat, "lon": lon, "type": row["type"],
                    "elev": int(float(row["elevation_ft"])) if row.get("elevation_ft") else None,
                    "sched": row.get("scheduled_service") == "yes",
                }
                if code in self.by_code:
                    continue
                self.by_code[code] = a
                self.grid[(math.floor(lat), math.floor(lon))].append(a)
        self.visited = set()
        self.habit = {}

    def near(self, lat, lon, r_nm):
        dlat = r_nm / 60
        dlon = r_nm / (60 * max(0.2, math.cos(math.radians(lat))))
        for i in range(math.floor(lat - dlat), math.floor(lat + dlat) + 1):
            for j in range(math.floor(lon - dlon), math.floor(lon + dlon) + 1):
                for a in self.grid.get((i, j), ()):
                    d = dist_nm(lat, lon, a["lat"], a["lon"])
                    if d <= r_nm:
                        yield a, d

    def at_ground(self, lat, lon):
        """Aeropuerto donde esta un punto en tierra (o pista sin registro)."""
        best, bd = None, 1e9
        for a, d in self.near(lat, lon, 3.0):
            score = d - {"large_airport": 0.9, "medium_airport": 0.6}.get(a["type"], 0)
            if score < bd:
                best, bd = a, score
        if best:
            return best
        code = f"P{lat:+.3f}{lon:+.3f}"
        if code not in self.by_code:
            ref = min(self.near(lat, lon, 60), key=lambda x: x[1], default=(None, None))[0]
            self.by_code[code] = {
                "code": code, "name": "Pista sin registro",
                "city": f"cerca de {ref['city'] or ref['name']}" if ref else "",
                "country": ref["country"] if ref else "", "iata": "", "lat": lat, "lon": lon,
                "type": "unlisted", "elev": None, "sched": False}
        return self.by_code[code]

    def weight(self, a):
        t = a["type"]
        if t == "large_airport":
            w = 1.0
        elif t == "medium_airport":
            w = 0.8
        elif a["sched"]:
            w = 0.5
        elif a["iata"]:
            w = 0.4
        elif re.fullmatch(r"(K[A-Z0-9]{3}|M[A-Z]{3}|C[A-Z]{3}|T[A-Z]{3})", a["code"]):
            w = 0.25
        else:
            w = 0.06
        if a["code"] in self.visited:
            w = max(w, 1.0) * 6
        elif a["code"] in self.habit:
            w = max(w, 0.8) * (1 + 0.6 * min(4, self.habit[a["code"]]))
        return w


# ---------------------------------------------------------- vuelos (legs)
GAP_SPLIT = 20 * 60


def is_ground(p):
    alt, gs = p[3], p[4]
    if alt == "g":
        return gs is None or gs < 100
    return gs is not None and gs < 35 and (alt is None or alt < 12000)


def clean(pts):
    out = []
    for i, p in enumerate(pts):
        if out:
            q = out[-1]
            dt = p[0] - q[0]
            if dt <= 0:
                continue
            v = dist_nm(q[1], q[2], p[1], p[2]) / (dt / 3600)
            if v > 450 and dt < 900 and i + 1 < len(pts):
                n = pts[i + 1]
                dt2 = n[0] - q[0]
                if dt2 > 0 and dist_nm(q[1], q[2], n[1], n[2]) / (dt2 / 3600) < 350:
                    continue
        out.append(p)
    return out


def stop_gap(a, b):
    dt = b[0] - a[0]
    if dt > 6 * 3600:
        return True
    v = dist_nm(a[1], a[2], b[1], b[2]) / (dt / 3600)
    return v < 115


class Leg:
    def __init__(self):
        self.pts = []
        self.dep_g = self.arr_g = self.dep_late = self.arr_late = None
        self.stop_before = self.stop_after = False
        self.dep = self.arr = None  # (airport, method, confidence)
        self.dep_gap = self.arr_gap = None
        self.dep_lim = self.arr_lim = None
        self.arr_prev = None


def build_legs(pts):
    legs, cur, last_g = [], None, None
    for p in pts:
        if is_ground(p):
            if cur is not None:
                if p[0] - cur.pts[-1][0] <= GAP_SPLIT:
                    cur.arr_g = p
                else:
                    cur.arr_late = p
                legs.append(cur)
                cur = None
            last_g = p
            continue
        if cur is None:
            cur = Leg()
            if last_g is not None:
                if p[0] - last_g[0] <= GAP_SPLIT:
                    cur.dep_g = last_g
                else:
                    cur.dep_late = last_g
            last_g = None
        elif p[0] - cur.pts[-1][0] > GAP_SPLIT and stop_gap(cur.pts[-1], p):
            cur.stop_after = True
            legs.append(cur)
            cur = Leg()
            cur.stop_before = True
        cur.pts.append(p)
    if cur is not None:
        legs.append(cur)
    out = []
    for L in legs:
        dur = L.pts[-1][0] - L.pts[0][0]
        span = dist_nm(L.pts[0][1], L.pts[0][2], L.pts[-1][1], L.pts[-1][2])
        top = max((p[3] for p in L.pts if isinstance(p[3], int)), default=0)
        if dur < 180 and span < 3 and top < 10000:
            continue  # ruido en tierra (rodaje, rebote de datos); nunca un tramo en crucero
        out.append(L)
    return out


def low_airport(p, aps):
    """Aeropuerto si el punto esta a <2.5 nm y <700 ft sobre el terreno."""
    if not isinstance(p[3], int):
        return None
    best, bd = None, 1e9
    for A, d in aps.near(p[1], p[2], 2.5):
        if A["elev"] is None or p[3] - A["elev"] > 700:
            continue
        score = d - {"large_airport": 0.9, "medium_airport": 0.6}.get(A["type"], 0)
        if score < bd:
            best, bd = A, score
    return best


def agl(p, a):
    alt = p[3] if isinstance(p[3], int) else None
    if alt is None:
        return 3000.0
    return max(0.0, alt - (a["elev"] if a["elev"] is not None else 500))


def arr_cost(A, p, t_max, aps, origin=None):
    d = dist_nm(p[1], p[2], A["lat"], A["lon"])
    h = agl(p, A)
    c = 0.0
    if p[5] is not None and d > 1.5:
        c += min(1.0, d / 8) * (angdiff(p[5], bearing(p[1], p[2], A["lat"], A["lon"])) / 35) ** 2
    d_nom, d_min = h / 1000 * 3.0, h / 1000 * 1.2
    if p[6] is not None and p[6] < -300:
        c += ((d - d_nom) / max(6.0, 0.4 * d_nom)) ** 2
    else:
        c += (max(0.0, d_min - d) / 4) ** 2 + d / 150
    if t_max is not None:
        need = d / block_speed(p) * 3600
        late = p[0] + need + 600 - t_max
        if late > 0:
            c += 25 + (late / 600) ** 2
    c += route_cost(origin, A, p)
    return c - 2 * math.log(aps.weight(A)), d


def dep_cost(A, q, t_min, aps, dest=None):
    d = dist_nm(A["lat"], A["lon"], q[1], q[2])
    h = agl(q, A)
    c = 0.0
    if q[5] is not None and d > 1.5:
        c += min(1.0, d / 8) * (angdiff(q[5], bearing(A["lat"], A["lon"], q[1], q[2])) / 35) ** 2
    d_nom, d_min = h / 1000 * 2.5, h / 1000 * 1.0
    if q[6] is not None and q[6] > 300:
        c += ((d - d_nom) / max(6.0, 0.4 * d_nom)) ** 2
    else:
        c += (max(0.0, d_min - d) / 4) ** 2 + d / 150
    if t_min is not None:
        need = d / block_speed(q) * 3600
        early = t_min + 600 - (q[0] - need)
        if early > 0:
            c += 25 + (early / 600) ** 2
    c += route_cost(A, dest, q)
    return c - 2 * math.log(aps.weight(A)), d


def block_speed(p):
    """Velocidad promedio (kt) para el tramo no observado de un PC-12."""
    return max(130.0, min(230.0, 0.8 * (p[4] or 200)))


def radius_for(p, t_other, default=350):
    if t_other is None:
        return default
    hrs = abs(t_other - p[0]) / 3600
    return max(25, min(default, hrs * 300))


ALTS = {}


def softmax_best(scored):
    """scored: [(cost, item)] -> (item, prob, cost). Guarda alternativas."""
    if not scored:
        return None, 0.0, None
    scored.sort(key=lambda x: x[0])
    c0 = scored[0][0]
    w = [math.exp(-(c - c0) / 2) for c, _ in scored[:200]]
    z = sum(w)
    ALTS[id(scored[0][1]), round(c0, 6)] = [(scored[i][1]["code"], w[i] / z) for i in range(1, min(4, len(w)))]
    return scored[0][1], 1 / z, c0


def alts_for(A, c):
    return [[k, round(v, 2)] for k, v in ALTS.get((id(A), round(c, 6)), []) if v >= 0.05]


def infer(legs, aps):
    """Dos pasadas: la segunda favorece los aeropuertos que el avion usa
    habitualmente (estimados con confianza en la primera)."""
    infer_pass(legs, aps)
    habit = defaultdict(int)
    for L in legs:
        for r in (L.dep, L.arr):
            if r and r[1] != "tierra" and r[2] >= 0.7:
                habit[r[0]["code"]] += 1
    aps.habit = dict(habit)
    for L in legs:
        L.arr_prev = L.arr
        if L.dep and L.dep[1] != "tierra":
            L.dep = None
        if L.arr and L.arr[1] != "tierra":
            L.arr = None
    infer_pass(legs, aps)


def infer_pass(legs, aps):
    # 1) aeropuertos observados en tierra
    for L in legs:
        if L.dep_g:
            L.dep = (aps.at_ground(L.dep_g[1], L.dep_g[2]), "tierra", 0.99, [])
        if L.arr_g:
            L.arr = (aps.at_ground(L.arr_g[1], L.arr_g[2]), "tierra", 0.99, [])
        # despegue/aterrizaje visto a muy baja altura sobre un aeropuerto
        if L.dep is None:
            A = low_airport(L.pts[0], aps)
            if A:
                L.dep = (A, "tierra", 0.97, [])
        if L.arr is None:
            A = low_airport(L.pts[-1], aps)
            if A:
                L.arr = (A, "tierra", 0.97, [])
        for g in (L.dep_g, L.arr_g, L.dep_late, L.arr_late):
            if g:
                aps.visited.add(aps.at_ground(g[1], g[2])["code"])
        for r in (L.dep, L.arr):
            if r and r[1] == "tierra":
                aps.visited.add(r[0]["code"])

    def known_dest(N):
        r = N.arr or N.arr_prev
        return r[0] if r else None

    def best_arr(L, t_max):
        p = L.pts[-1]
        o = L.dep[0] if L.dep else None
        sc = [(arr_cost(A, p, t_max, aps, o)[0], A) for A, _ in aps.near(p[1], p[2], radius_for(p, t_max))]
        return softmax_best(sc)

    def best_dep(L, t_min):
        q = L.pts[0]
        dst = known_dest(L)
        sc = [(dep_cost(A, q, t_min, aps, dst)[0], A) for A, _ in aps.near(q[1], q[2], radius_for(q, t_min))]
        return softmax_best(sc)

    # 2) fronteras entre vuelos consecutivos
    for i in range(len(legs) + 1):
        P = legs[i - 1] if i > 0 else None
        N = legs[i] if i < len(legs) else None
        need_arr = P is not None and P.arr is None
        need_dep = N is not None and N.dep is None
        if not need_arr and not need_dep:
            continue
        if need_arr and need_dep:
            p, q = P.pts[-1], N.pts[0]
            o, dst = (P.dep[0] if P.dep else None), known_dest(N)
            # candidato conjunto: el mismo aeropuerto para aterrizar y despegar
            seen = {}
            for A, _ in aps.near(p[1], p[2], radius_for(p, q[0])):
                seen[A["code"]] = A
            for A, _ in aps.near(q[1], q[2], radius_for(q, p[0])):
                seen[A["code"]] = A
            # aduana: entre un origen y un destino de paises distintos, la
            # escala es del lado del destino (entrada a EUA en aeropuerto con aduana)
            c_from = o["country"] if o else None
            c_to = dst["country"] if dst else None
            joint = []
            for A in seen.values():
                ca, da = arr_cost(A, p, None, aps, o)
                cd, dd = dep_cost(A, q, None, aps, dst)
                # ida + vuelta a velocidad promedio realista (ascenso/descenso
                # incluidos) + 15 min minimos en tierra
                travel = (da / block_speed(p) + dd / block_speed(q)) * 3600 + 900
                slack = (q[0] - p[0]) - travel
                pen = 0 if slack >= 0 else 8 + (slack / 300) ** 2
                if c_from and c_to and c_from != c_to:
                    pen += -1.5 if A["country"] == c_to else 1.5
                joint.append((ca + cd + pen + 2 * math.log(aps.weight(A)), A))
            Aj, pj, cj = softmax_best(joint)
            alt_j = alts_for(Aj, cj) if Aj else []
            P.arr_gap = N.dep_gap = q[0] - p[0]
            if Aj is not None:
                ta = dist_nm(p[1], p[2], Aj["lat"], Aj["lon"]) / block_speed(p) * 3600
                td = dist_nm(Aj["lat"], Aj["lon"], q[1], q[2]) / block_speed(q) * 3600
                window = q[0] - p[0] - 600
                if ta + td > window > 0:
                    k = window / (ta + td)
                    ta, td = ta * k, td * k
                P.arr_lim = int(p[0] + ta)
                N.dep_lim = int(q[0] - td)
            if P.stop_after and N.stop_before:
                P.arr = (Aj, "escala", pj, alt_j)
                N.dep = (Aj, "escala", pj, alt_j)
                continue
            Aa, pa, ca = best_arr(P, q[0])
            alt_a = alts_for(Aa, ca) if Aa else []
            Ad, pd, cd = best_dep(N, p[0])
            alt_d = alts_for(Ad, cd) if Ad else []
            if Aj is not None and ca is not None and cd is not None and cj <= ca + cd + 4:
                P.arr = (Aj, "secuencia", pj, alt_j)
                N.dep = (Aj, "secuencia", pj, alt_j)
            else:
                P.arr = (Aa, "trayectoria", pa, alt_a) if Aa else None
                N.dep = (Ad, "trayectoria", pd, alt_d) if Ad else None
            continue
        if need_arr:
            p = P.pts[-1]
            o = P.dep[0] if P.dep else None
            t_max = N.pts[0][0] if N else None
            Aa, pa, ca = best_arr(P, t_max)
            alt_a = alts_for(Aa, ca) if Aa else []
            chosen = None
            ref = None
            if P.arr_late:
                ref = aps.at_ground(P.arr_late[1], P.arr_late[2])
            elif N is not None and N.dep is not None:
                ref = N.dep[0]
            if ref is not None and ca is not None:
                cr = arr_cost(ref, p, t_max, aps, o)[0]
                if cr <= ca + 4:
                    chosen = (ref, "secuencia", max(0.6, min(0.95, math.exp(-(cr - ca) / 2))), [])
            P.arr = chosen or ((Aa, "trayectoria", pa, alt_a) if Aa else None)
        if need_dep:
            q = N.pts[0]
            dst = known_dest(N)
            t_min = P.pts[-1][0] if P else None
            Ad, pd, cd = best_dep(N, t_min)
            alt_d = alts_for(Ad, cd) if Ad else []
            chosen = None
            ref = None
            if N.dep_late:
                ref = aps.at_ground(N.dep_late[1], N.dep_late[2])
            elif P is not None and P.arr is not None:
                ref = P.arr[0]
            if ref is not None and cd is not None:
                cr = dep_cost(ref, q, t_min, aps, dst)[0]
                if cr <= cd + 4:
                    chosen = (ref, "secuencia", max(0.6, min(0.95, math.exp(-(cr - cd) / 2))), [])
            N.dep = chosen or ((Ad, "trayectoria", pd, alt_d) if Ad else None)


def leg_json(L, data_end):
    p0, p1 = L.pts[0], L.pts[-1]
    dep, arr = L.dep, L.arr
    # tiempos de despegue/aterrizaje
    if dep and dep[1] == "tierra":
        off, off_e = p0[0], False
    elif dep:
        off = int(p0[0] - dist_nm(dep[0]["lat"], dep[0]["lon"], p0[1], p0[2]) / block_speed(p0) * 3600)
        if L.dep_lim is not None:
            off = max(off, L.dep_lim)
        off_e = True
    else:
        off, off_e = p0[0], True
    if arr and arr[1] == "tierra":
        on, on_e = p1[0], False
    elif arr:
        on = int(p1[0] + dist_nm(p1[1], p1[2], arr[0]["lat"], arr[0]["lon"]) / block_speed(p1) * 3600)
        if L.arr_lim is not None:
            on = min(on, L.arr_lim)
        on_e = True
    else:
        on, on_e = p1[0], True
    # trayectoria observada, separada en huecos de cobertura > 10 min
    segs, seg = [], []
    nm = 0.0
    for a, b in zip(L.pts, L.pts[1:]):
        nm += dist_nm(a[1], a[2], b[1], b[2])
    for p in L.pts:
        if seg and p[0] - seg[-1][2] > 600:
            segs.append(seg)
            seg = []
        seg.append((p[1], p[2], p[0]))
    if seg:
        segs.append(seg)
    out_segs = [[[round(x[0], 4), round(x[1], 4)] for x in rdp(s, 0.25)] for s in segs]
    if dep:
        nm += dist_nm(dep[0]["lat"], dep[0]["lon"], p0[1], p0[2]) if dep[1] != "tierra" else 0
    if arr:
        nm += dist_nm(p1[1], p1[2], arr[0]["lat"], arr[0]["lon"]) if arr[1] != "tierra" else 0
    alts = [p[3] for p in L.pts if isinstance(p[3], int)]

    def geo(a, p):
        if not a:
            return None
        d = dist_nm(a["lat"], a["lon"], p[1], p[2])
        b = bearing(a["lat"], a["lon"], p[1], p[2])
        card = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"][int((b + 22.5) // 45) % 8]
        return [round(d, 1), p[3] if isinstance(p[3], int) else 0, card]
    j = {
        "id": p0[0], "off": int(off), "on": int(on), "offE": off_e, "onE": on_e,
        "dep": dep[0]["code"] if dep else None, "arr": arr[0]["code"] if arr else None,
        "depM": dep[1] if dep else None, "arrM": arr[1] if arr else None,
        "depC": round(dep[2], 2) if dep else 0, "arrC": round(arr[2], 2) if arr else 0,
        "seen": [p0[0], p1[0]], "alt": max(alts) if alts else None, "nm": round(nm),
        "segs": out_segs,
    }
    if dep and dep[1] != "tierra" and dep[2] < 0.8 and dep[3]:
        j["depAlt"] = dep[3]
    if arr and arr[1] != "tierra" and arr[2] < 0.8 and arr[3]:
        j["arrAlt"] = arr[3]
    if dep and dep[1] != "tierra":
        j["depX"] = geo(dep[0], p0)
        if L.dep_gap:
            j["depGap"] = round(L.dep_gap / 60)
    if arr and arr[1] != "tierra":
        j["arrX"] = geo(arr[0], p1)
        if L.arr_gap:
            j["arrGap"] = round(L.arr_gap / 60)
    if not (arr and arr[1] == "tierra") and data_end - p1[0] < 900:
        j["open"] = True  # la traza termina al final de los datos (vuelo en curso)
    return j


def build_flights(days, aps):
    pts = []
    for day, st in sorted(days.items()):
        if st.get("status") != "ok":
            continue
        try:
            with gzip.open(os.path.join(RAW, f"{day}.json.gz"), "rt", encoding="utf-8") as f:
                pts += json.load(f)["points"]
        except FileNotFoundError:
            continue
    pts.sort(key=lambda p: p[0])
    dedup = []
    for p in pts:
        if not dedup or p[0] != dedup[-1][0]:
            dedup.append(tuple(p))
    pts = clean(dedup)
    legs = build_legs(pts)
    infer(legs, aps)
    ok_days = [d for d, s in days.items() if s.get("status") == "ok"]
    last_day = max(ok_days) if ok_days else None
    data_end = (int(datetime.fromisoformat(last_day).replace(tzinfo=timezone.utc).timestamp())
                + 86400) if last_day else 0
    flights = [leg_json(L, data_end) for L in legs]
    flights.sort(key=lambda f: f["off"], reverse=True)
    used = {}
    for f in flights:
        for c in [f["dep"], f["arr"]] + [a[0] for a in f.get("depAlt", []) + f.get("arrAlt", [])]:
            if c and c not in used:
                a = aps.by_code[c]
                used[c] = {k: a[k] for k in ("name", "city", "country", "iata", "lat", "lon", "type", "elev")}
    return flights, used, pts


def live_status(aps, pts):
    now = int(time.time())
    cur = None
    try:
        with http(f"https://api.adsb.lol/v2/hex/{ICAO}", timeout=30) as r:
            ac = (json.load(r).get("ac") or [None])[0]
        if ac and ac.get("lat") is not None:
            g = ac.get("alt_baro") == "ground"
            cur = {"t": now - int(ac.get("seen_pos") or ac.get("seen") or 0),
                   "lat": ac["lat"], "lon": ac["lon"], "ground": g,
                   "alt": None if g else ac.get("alt_baro"), "gs": ac.get("gs"),
                   "trk": ac.get("track") or ac.get("true_heading")}
    except Exception as e:  # noqa: BLE001
        print(f"API en vivo: {e}")
    if cur is None and pts:
        p = pts[-1]
        cur = {"t": p[0], "lat": p[1], "lon": p[2], "ground": p[3] == "g" or is_ground(p),
               "alt": p[3] if isinstance(p[3], int) else None, "gs": p[4], "trk": p[5],
               "historic": True}
    if cur and cur["ground"]:
        a = aps.at_ground(cur["lat"], cur["lon"])
        cur["ap"] = a["code"]
        cur["apName"] = a["name"]
        cur["apCity"] = a["city"]
    return {"checked": now, "last": cur}


def main():
    today = datetime.now(timezone.utc).date()
    skip_dl = os.environ.get("SKIP_DOWNLOAD") == "1"
    days = load(F_DAYS, {}) if skip_dl else download(today)[0]
    aps = Airports()
    flights, used, pts = build_flights(days, aps)
    save(F_FLIGHTS, flights, compact=True)
    save(F_AIRPORTS, dict(sorted(used.items())))
    done = sorted(d for d, s in days.items() if s.get("status") in ("ok", "none", "missing"))
    start = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    pend = [d for d in ((today - timedelta(days=i)).isoformat() for i in range(1, HISTORY_DAYS + 1))
            if days.get(d, {}).get("status") not in ("ok", "none", "missing")]
    # periodos largos sin ninguna posicion del avion (>= 14 dias seguidos)
    gaps, run = [], []
    for d in sorted(days):
        if days[d].get("status") in ("none", "missing"):
            run.append(d)
        else:
            if len(run) >= 14:
                gaps.append([run[0], run[-1]])
            run = []
    if len(run) >= 14:
        gaps.append([run[0], run[-1]])
    meta = {
        "updated": int(time.time()),
        "gaps": gaps,
        "source": "adsb.lol (ODbL 1.0)",
        "from": done[0] if done else None,
        "to": done[-1] if done else None,
        "target_from": start,
        "days_ok": sum(1 for s in days.values() if s.get("status") == "ok"),
        "days_pending": len(pend),
        "complete": len(pend) <= 2,
        "flights": len(flights),
    }
    save_if_changed(F_META, meta, ignore=("updated",))
    live = live_status(aps, pts)
    old_live = load(F_LIVE, {})
    if (old_live.get("last") or {}) != (live.get("last") or {}):
        save(F_LIVE, live)
    print(f"Vuelos: {len(flights)} | dias con datos: {meta['days_ok']} | "
          f"pendientes: {len(pend)} | rango {meta['from']} a {meta['to']}")


if __name__ == "__main__":
    sys.exit(main())
