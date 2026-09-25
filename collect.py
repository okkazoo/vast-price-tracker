"""Hourly Vast.ai on-demand price snapshot for RTX 3090 / 4090 / 5090.

Stdlib only. Appends long-format stats rows to data/prices.csv, one line per
run to data/collect.log, and a compact raw snapshot to data/raw/YYYY-MM-DD.jsonl.gz.

Exit codes: 0 ok, 2 network/API failure (nothing written to prices.csv).

Test-only override: VPT_RESOLVE=<ip> makes console.vast.ai resolve to that IP
(TLS SNI/Host stay console.vast.ai). Never used unless explicitly set.
"""
import csv
import datetime as dt
import gzip
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOST = "console.vast.ai"
URL = f"https://{HOST}/api/v0/search/asks/"
GPUS = ["RTX 3090", "RTX 4090", "RTX 5090"]
RELIABLE_MIN = 0.98
RETRIES = 3
TIMEOUT = 30

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CSV_PATH = DATA / "prices.csv"
LOG_PATH = DATA / "collect.log"
RAW_DIR = DATA / "raw"
KEY_PATH = Path.home() / ".config" / "vastai" / "vast_api_key"

COLUMNS = ["ts_utc", "gpu", "subset", "n_offers", "n_gpus", "min", "p10", "p25",
           "median", "p75", "p90", "mean", "cheap5", "gpu_weighted_median"]


class DNSUnavailable(Exception):
    pass


def _install_test_resolve():
    ip = os.environ.get("VPT_RESOLVE", "").strip()
    if not ip:
        return
    real = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        if host == HOST:
            host = ip
        return real(host, *args, **kwargs)

    socket.getaddrinfo = patched


def _api_key():
    try:
        key = KEY_PATH.read_text(encoding="utf-8").strip()
        return key or None
    except OSError:
        return None


def _is_dns_error(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, socket.gaierror) or isinstance(exc, socket.gaierror)


def fetch_offers(gpu):
    body = json.dumps({"q": {"rentable": {"eq": True}, "gpu_name": {"eq": gpu},
                             "type": "on-demand", "limit": 5000}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "vast-price-tracker/1.0"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    last = None
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(2 ** attempt)  # 2s, 4s
        try:
            req = urllib.request.Request(URL, data=body, headers=headers, method="PUT")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            offers = payload.get("offers")
            if not isinstance(offers, list):
                raise ValueError("response has no 'offers' list")
            return offers
        except Exception as exc:  # noqa: BLE001 - retry anything, report the last
            last = exc
    if _is_dns_error(last):
        raise DNSUnavailable(str(last))
    raise RuntimeError(f"{gpu}: {type(last).__name__}: {last}")


def percentile(sorted_vals, q):
    """Linear-interpolated percentile (numpy default), q in [0, 100]."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def weighted_median(pairs):
    """pairs: [(price, weight)]. Lower weighted median."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    cum = 0
    for price, w in pairs:
        cum += w
        if cum >= total / 2.0:
            return price
    return pairs[-1][0]


def stats(rows):
    """rows: list of (per_gpu_price, num_gpus)."""
    out = {"n_offers": len(rows), "n_gpus": sum(n for _, n in rows)}
    if not rows:
        for c in COLUMNS[5:]:
            out[c] = ""
        return out
    prices = sorted(p for p, _ in rows)
    r = lambda x: round(x, 4)  # noqa: E731
    out.update({
        "min": r(prices[0]),
        "p10": r(percentile(prices, 10)),
        "p25": r(percentile(prices, 25)),
        "median": r(percentile(prices, 50)),
        "p75": r(percentile(prices, 75)),
        "p90": r(percentile(prices, 90)),
        "mean": r(sum(prices) / len(prices)),
        "cheap5": r(sum(prices[:5]) / len(prices[:5])),
        "gpu_weighted_median": r(weighted_median(rows)),
    })
    return out


def normalise(offers):
    """-> list of dicts with per-GPU price; drops offers without usable price."""
    out = []
    for o in offers:
        try:
            n = int(o.get("num_gpus") or 0)
            dph = float(o.get("dph_total"))
        except (TypeError, ValueError):
            continue
        if n <= 0 or dph <= 0:
            continue
        out.append({"id": o.get("id"), "machine_id": o.get("machine_id"),
                    "price": dph / n, "num_gpus": n,
                    "rel": float(o.get("reliability2") or 0.0),
                    "geo": o.get("geolocation")})
    return out


def log(line):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def main():
    _install_test_resolve()
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    results = {}
    try:
        for gpu in GPUS:
            results[gpu] = normalise(fetch_offers(gpu))
    except DNSUnavailable:
        log(f"{ts} SKIP network/DNS unavailable - skipped")
        return 2
    except Exception as exc:  # noqa: BLE001
        log(f"{ts} FAIL {exc} - nothing written")
        return 2

    rows = []
    for gpu in GPUS:
        offers = results[gpu]
        subsets = {"all": offers, "reliable": [o for o in offers if o["rel"] >= RELIABLE_MIN]}
        for name, subset in subsets.items():
            row = {"ts_utc": ts, "gpu": gpu, "subset": name}
            row.update(stats([(o["price"], o["num_gpus"]) for o in subset]))
            rows.append(row)

    DATA.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            w.writeheader()
        w.writerows(rows)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    snap = {"ts": ts, "offers": [
        [gpu, o["id"], o["machine_id"], round(o["price"], 4), o["num_gpus"],
         round(o["rel"], 4), o["geo"]]
        for gpu in GPUS for o in results[gpu]]}
    with gzip.open(RAW_DIR / f"{ts[:10]}.jsonl.gz", "at", encoding="utf-8") as f:
        f.write(json.dumps(snap, separators=(",", ":")) + "\n")

    med = " ".join(
        f"{g.split()[-1]}={next(r['median'] for r in rows if r['gpu'] == g and r['subset'] == 'all')}"
        f"(n={len(results[g])})" for g in GPUS)
    log(f"{ts} OK median/gpu/hr {med}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
