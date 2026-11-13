#!/usr/bin/env python3
"""Diagnostico 2: orden interno de los releases diarios de adsb.lol (para leer
solo el archivo del avion con peticiones Range) y API en vivo de adsb.lol."""
import json
import os
import time
import urllib.error
import urllib.request

ICAO = "0d0929"
UA = {"User-Agent": "xa-uwk-history/1.0 (github.com/juanpablogza1/XA-UWK)"}


def http(url, headers=None, timeout=60):
    h = dict(UA)
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)


def assets(d):
    tag = f"v{d.replace('-', '.')}-planes-readsb-prod-0"
    hdr = {"Accept": "application/vnd.github+json",
           "Authorization": f"Bearer {os.environ['GH_TOKEN']}"}
    with http(f"https://api.github.com/repos/adsblol/globe_history_{d[:4]}/releases/tags/{tag}",
              headers=hdr) as r:
        rel = json.load(r)
    a = sorted([x for x in rel["assets"] if ".tar" in x["name"]], key=lambda x: x["name"])
    return [(x["browser_download_url"], x["size"]) for x in a]


def read_range(parts, off, n):
    out = b""
    base = 0
    for url, size in parts:
        if off < base + size and off + n > base:
            s = max(off, base) - base
            e = min(off + n, base + size) - base - 1
            with http(url, headers={"Range": f"bytes={s}-{e}"}) as r:
                out += r.read()
        base += size
    return out


def parse_header(blk):
    if len(blk) < 512 or blk[257:262] != b"ustar":
        return None
    chk = sum(blk[:148]) + 8 * 32 + sum(blk[156:512])
    try:
        stored = int(blk[148:156].split(b"\0")[0].strip() or b"0", 8)
        size = int(blk[124:136].split(b"\0")[0].strip() or b"0", 8)
    except ValueError:
        return None
    if chk != stored:
        return None
    name = blk[0:100].split(b"\0")[0].decode("utf-8", "replace")
    prefix = blk[345:500].split(b"\0")[0].decode("utf-8", "replace")
    return (prefix + "/" + name if prefix else name), size


def probe(parts, off, window=262144):
    off -= off % 512
    data = read_range(parts, off, window)
    for i in range(0, len(data) - 511, 512):
        h = parse_header(data[i:i + 512])
        if h:
            return off + i, h[0], h[1]
    return None


def main():
    d = "2026-11-12"
    parts = assets(d)
    total = sum(s for _, s in parts)
    print(f"{d}: total {total / 2**30:.2f} GiB en {len(parts)} partes")
    t0 = time.time()
    # primeros miembros
    off = 0
    for _ in range(12):
        blk = read_range(parts, off, 512)
        h = parse_header(blk)
        if not h:
            print("  sin header en", off)
            break
        print(f"  @{off:>12} {h[0]} ({h[1]} B)")
        off += 512 + ((h[1] + 511) // 512) * 512
    # sondeos uniformes
    for k in range(1, 48):
        p = probe(parts, total * k // 48)
        print(f"  {k:2d}/48 @{(p[0] / 2**30) if p else 0:6.3f} GiB  {p[1] if p else '-'}")
    print(f"  sondeos en {time.time() - t0:.1f}s")
    # API en vivo de adsb.lol
    for url in (f"https://api.adsb.lol/v2/hex/{ICAO}", f"https://api.adsb.lol/v2/icao/{ICAO}"):
        try:
            with http(url, headers={"Origin": "https://juanpablogza1.github.io"}) as r:
                body = r.read()[:300]
                print(url, r.status, "ACAO=", r.headers.get("Access-Control-Allow-Origin"), body)
        except urllib.error.HTTPError as e:
            print(url, "HTTP", e.code)
        except Exception as e:  # noqa
            print(url, e)


if __name__ == "__main__":
    main()
