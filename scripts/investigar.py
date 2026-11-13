#!/usr/bin/env python3
"""Diagnostico: que fuentes de trayectoria existen para los vuelos de XA-UWK.

1. OpenSky /tracks/all para los vuelos de los ultimos 29 dias.
2. adsb.lol: historial web (globe_history) y releases diarios en GitHub.
Resultados en out/ (se suben como artifact).
"""
import gzip
import io
import json
import os
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

ICAO = "0d0929"
OUT = "out"
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "xa-uwk-history/1.0 (github.com/juanpablogza1/XA-UWK)"}


def http(url, headers=None, data=None, timeout=60):
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data)
    return urllib.request.urlopen(req, timeout=timeout)


def token():
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": os.environ["OPENSKY_CLIENT_ID"],
        "client_secret": os.environ["OPENSKY_CLIENT_SECRET"]}).encode()
    with http("https://auth.opensky-network.org/auth/realms/opensky-network/"
              "protocol/openid-connect/token", data=body,
              headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
        return json.load(r)["access_token"]


def day(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def opensky_tracks(flights):
    tok = token()
    now = time.time()
    res = {}
    for f in flights:
        if now - f["firstSeen"] > 29 * 86400:
            continue
        mid = (f["firstSeen"] + f["lastSeen"]) // 2
        url = f"https://opensky-network.org/api/tracks/all?icao24={ICAO}&time={mid}"
        try:
            with http(url, headers={"Authorization": f"Bearer {tok}"}) as r:
                tr = json.load(r)
            p = tr.get("path") or []
            res[str(f["firstSeen"])] = tr
            print(f"OS track {day(f['firstSeen'])} {f['dep']}->{f['arr']}: {len(p)} pts"
                  f" first={p[0] if p else None} last={p[-1] if p else None}")
        except urllib.error.HTTPError as e:
            print(f"OS track {day(f['firstSeen'])}: HTTP {e.code} {e.read()[:120]!r}")
        time.sleep(1)
    json.dump(res, open(f"{OUT}/opensky_tracks.json", "w"))


def summarize_trace(raw, label):
    try:
        data = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    except Exception as e:  # noqa
        print(f"{label}: no es JSON ({e}); primeros bytes {raw[:80]!r}")
        return None
    tr = data.get("trace") or []
    ts = data.get("timestamp", 0)
    print(f"{label}: OK {len(tr)} puntos, r={data.get('r')} t={data.get('t')}")
    # segmentos separados por huecos > 20 min o por 'ground'
    seg_start = None
    prev = None
    for p in tr:
        t = ts + p[0]
        if prev is None or t - prev > 1200:
            if seg_start is not None:
                print(f"   tramo {time.strftime('%H:%M', time.gmtime(seg_start[0]))}"
                      f" ({seg_start[1]:.3f},{seg_start[2]:.3f},{seg_start[3]}) -> "
                      f"{time.strftime('%H:%M', time.gmtime(prev_p[0]))}"
                      f" ({prev_p[1]:.3f},{prev_p[2]:.3f},{prev_p[3]})")
            seg_start = (t, p[1], p[2], p[3])
        prev = t
        prev_p = (t, p[1], p[2], p[3])
    if seg_start is not None:
        print(f"   tramo {time.strftime('%H:%M', time.gmtime(seg_start[0]))}"
              f" ({seg_start[1]:.3f},{seg_start[2]:.3f},{seg_start[3]}) -> "
              f"{time.strftime('%H:%M', time.gmtime(prev_p[0]))}"
              f" ({prev_p[1]:.3f},{prev_p[2]:.3f},{prev_p[3]})")
    return data


def web_history(d):
    y, m, dd = d.split("-")
    for base in ("https://adsb.lol", "https://globe.adsb.lol"):
        url = f"{base}/globe_history/{y}/{m}/{dd}/traces/29/trace_full_{ICAO}.json"
        try:
            with http(url, headers={"Referer": base + "/", "Accept-Encoding": "gzip"},
                      timeout=30) as r:
                raw = r.read()
            data = summarize_trace(raw, f"web {base} {d}")
            if data:
                json.dump(data, open(f"{OUT}/web_{d}.json", "w"))
                return True
        except urllib.error.HTTPError as e:
            print(f"web {base} {d}: HTTP {e.code}")
        except Exception as e:  # noqa
            print(f"web {base} {d}: {e}")
    return False


class Chain(io.RawIOBase):
    def __init__(self, urls):
        self.urls = list(urls)
        self.cur = None
        self.n = 0

    def readable(self):
        return True

    def readinto(self, b):
        while True:
            if self.cur is None:
                if not self.urls:
                    return 0
                self.cur = http(self.urls.pop(0), timeout=120)
            k = self.cur.readinto(b)
            if k:
                self.n += k
                return k
            self.cur.close()
            self.cur = None


def gh_release_assets(d):
    tag = f"v{d.replace('-', '.')}-planes-readsb-prod-0"
    repo = f"adsblol/globe_history_{d[:4]}"
    hdr = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GH_TOKEN"):
        hdr["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    try:
        with http(f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
                  headers=hdr) as r:
            rel = json.load(r)
        assets = sorted(rel["assets"], key=lambda a: a["name"])
        print(f"release {tag}: " + ", ".join(
            f"{a['name']} {a['size'] / 2**30:.2f} GiB" for a in assets))
        return [a["browser_download_url"] for a in assets if ".tar" in a["name"]]
    except urllib.error.HTTPError as e:
        print(f"release {tag}: HTTP {e.code}")
        return []


def gh_release_trace(d, limit_s=900):
    urls = gh_release_assets(d)
    if not urls:
        return False
    t0 = time.time()
    stream = io.BufferedReader(Chain(urls), buffer_size=4 << 20)
    found = False
    n = 0
    try:
        with tarfile.open(fileobj=stream, mode="r|") as tf:
            for m in tf:
                n += 1
                if m.name.endswith(f"trace_full_{ICAO}.json"):
                    raw = tf.extractfile(m).read()
                    data = summarize_trace(raw, f"release {d} ({m.name})")
                    if data:
                        json.dump(data, open(f"{OUT}/release_{d}.json", "w"))
                    found = True
                    break
                if time.time() - t0 > limit_s:
                    print("tiempo agotado")
                    break
    except Exception as e:  # noqa
        print(f"release {d}: error {e}")
    mb = stream.raw.n / 2**20
    print(f"release {d}: {'encontrado' if found else 'no encontrado'} tras {n} archivos,"
          f" {mb:.0f} MiB en {time.time() - t0:.0f}s")
    return found


def main():
    flights = json.load(open("data/flights.json"))
    print(f"{len(flights)} vuelos")
    try:
        opensky_tracks(flights)
    except Exception as e:  # noqa
        print(f"OpenSky tracks error: {e}")
    # dias de prueba: uno reciente y dos antiguos con aeropuertos nulos
    unknown_days = []
    for f in flights:
        if f["dep"] is None or f["arr"] is None:
            d = day(f["firstSeen"])
            if d not in unknown_days:
                unknown_days.append(d)
    sample = unknown_days[:1] + unknown_days[len(unknown_days) // 2:][:1] + unknown_days[-1:]
    print("dias de prueba:", sample)
    web_ok = [web_history(d) for d in sample]
    if not any(web_ok):
        gh_release_trace(sample[0])


if __name__ == "__main__":
    main()
