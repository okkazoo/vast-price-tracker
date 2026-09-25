"""Hourly Vast.ai on-demand price snapshot for RTX 3090 / 4090 / 5090.

Stdlib only. Appends long-format stats rows to data/prices.csv, one line per
run to data/collect.log, and a compact raw snapshot to data/raw/YYYY-MM-DD.jsonl.gz.
Also, for each card in cards.json, runs the dashboard's Offers-page search for that
card (one query per card) and appends subset "card:<name>" rows per GPU type plus a
gpu "ALL" row (see the card section below and README).

Exit codes: 0 ok, 2 network/API failure or capped unauthenticated result
(nothing written to prices.csv).

API key lookup: env VAST_API_KEY, then ~/.config/vastai/vast_api_key. Without a
key Vast caps search at 64 offers; a keyless run that hits the cap is discarded.

Test-only override: VPT_RESOLVE=<ip> makes console.vast.ai resolve to that IP
(TLS SNI/Host stay console.vast.ai). Never used unless explicitly set.
"""
import csv
import datetime as dt
import gzip
import json
import os
import re
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
UNAUTH_CAP = 64  # Vast returns at most this many offers without an API key
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
    key = os.environ.get("VAST_API_KEY", "").strip()
    if key:
        return key
    try:
        key = KEY_PATH.read_text(encoding="utf-8").strip()
        return key or None
    except OSError:
        return None


def _is_dns_error(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, socket.gaierror) or isinstance(exc, socket.gaierror)


def fetch_offers(gpu):
    return _search({"rentable": {"eq": True}, "gpu_name": {"eq": gpu},
                    "type": "on-demand", "limit": 5000}, gpu)


def _search(q, label):
    body = json.dumps({"q": q}).encode()
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
    raise RuntimeError(f"{label}: {type(last).__name__}: {last}")


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


# --- Per-card subsets: a port of the dashboard's Offers-page search -------------
# Source: vastai-api-manager dashboard/backend/routes/cards.py search_card_offers
# (+ get_search_params_for_card) and src/vastai_manager/providers/vastai.py
# search_offers, called with the params the Offers page sends for a freshly
# opened card (card max_price / min_disk_gb / min_download_speed, datacenter off,
# preferred_only on with the card's gpu_types, verified per card/settings,
# favorites passed, blocked hidden). One API query per card. Not mirrored (runtime
# or launch-time state the dashboard keeps in memory / a rolling ledger):
# anti-phantom ask-table reconciliation, dead-offer/dead-machine caches, the
# soft blocklist from launch_outcomes.json, and min_cpu_features.
CARDS_PATH = ROOT / "cards.json"

# provider CARD_TO_VASTAI_MAPPING: friendly key -> (vast field, op)
_CARD_MAP = {
    "min_vram_gb": ("gpu_ram", "gte"), "min_gpu_count": ("num_gpus", "gte"),
    "min_disk_gb": ("disk_space", "gte"), "min_disk_bw": ("disk_bw", "gte"),
    "min_cuda_version": ("cuda_vers", "gte"), "min_driver_version": ("driver_version", "gte"),
    "min_cuda_max_good": ("cuda_max_good", "gte"), "min_compute_cap": ("compute_cap", "gte"),
    "require_avx": ("has_avx", "eq"), "min_cpu_cores": ("cpu_cores", "gte"),
    "min_cpu_ghz": ("cpu_ghz", "gte"), "min_cpu_ram_gb": ("cpu_ram", "gte"),
    "min_upload_speed": ("inet_up", "gte"), "min_download_speed": ("inet_down", "gte"),
    "min_pcie_bw": ("pcie_bw", "gte"), "min_reliability": ("reliability", "gte"),
    "min_duration_days": ("duration", "gte"), "max_price": ("dph", "lte"),
    "datacenter": ("datacenter", "eq"), "static_ip": ("static_ip", "eq"),
    "geolocation": ("geolocation", "eq"),
}
_GB_TO_MB = {"min_vram_gb", "min_cpu_ram_gb"}  # kwargs path only
_DIRECT = {"gpu_ram", "gpu_name", "num_gpus", "disk_space", "disk_bw", "cuda_vers",
           "driver_version", "compute_cap", "cpu_cores", "cpu_cores_effective", "cpu_ram",
           "cpu_ghz", "inet_up", "inet_down", "pcie_bw", "bw_nvlink", "reliability", "duration",
           "dph", "total_flops", "datacenter", "static_ip", "geolocation", "gpu_arch", "cpu_arch",
           "dlperf", "direct_port_count", "ubuntu_version", "machine_id", "host_id",
           "verified", "external", "rentable", "rented"}
_EQ_DIRECT = {"machine_id", "host_id", "ubuntu_version", "geolocation"}
# search_offers named parameters (these skip the GB->MB hook: gpu_ram gets raw GB)
_NAMED = {"min_vram_gb": "gpu_ram", "min_disk_gb": "disk_space", "min_upload_speed": "inet_up",
          "min_download_speed": "inet_down", "min_reliability": "reliability"}
# host-viability floors re-applied after the merge: card key, row key, scale
_FLOORS = [("min_cpu_ram_gb", "cpu_ram", 1000), ("min_vram_gb", "gpu_ram", 1000),
           ("min_download_speed", "inet_down", 1), ("min_upload_speed", "inet_up", 1),
           ("min_cpu_ghz", "cpu_ghz", 1), ("min_cpu_cores", "cpu_cores_effective", 1),
           ("min_disk_bw", "disk_bw", 1), ("min_reliability", "reliability2", 1)]
_PRE_AVX2 = [re.compile(p, re.I) for p in (
    r"\bi[3579]-\d{3}[A-Z]{0,2}\b", r"\bi[3579]-[23]\d{3}[A-Z]{0,2}\b", r"\b[EXLW][3567]\d{3}\b",
    r"\bcore\s*2\b", r"\b(pentium|celeron|atom)\b", r"\b(fx-?\d{4}|phenom|athlon|opteron)\b")]
_XEON_E = re.compile(r"\bE[357]-\d{4}[A-Z]{0,2}(?:\s*[vV](\d+))?\b")


def load_cards():
    try:
        doc = json.loads(CARDS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], {}
    return doc.get("cards") or [], doc.get("dashboard") or {}


def card_params(card, dash):
    """get_search_params_for_card + the Offers-page overrides. -> (search_params, gate_reqs, ctx)."""
    reqs = dict(card.get("gpu_requirements") or {})
    sp = dict(reqs)
    sp.setdefault("max_price", 1.0)
    sp.setdefault("min_upload_speed", 500)
    sp.setdefault("min_download_speed", 500)
    sp.setdefault("gpu_count", 1)
    if not sp.get("datacenter"):
        sp.pop("datacenter", None)
    for k, v in (card.get("box_profile_gates") or {}).items():
        if k == "require_avx" and v:
            sp["require_avx"] = True
        elif k == "min_cuda_max_good" and (sp.get(k) is None or sp[k] < v):
            sp[k] = v
    sp = {k: v for k, v in sp.items() if v is not None}
    gate = dict(sp)  # _gate_reqs: the host gate re-reads get_search_params_for_card

    settings = dash.get("settings") or {}
    client_max = sp.pop("max_price", None)
    # Offers page sends the card's values explicitly (gpu.min_disk_gb || 50,
    # gpu.min_download_speed ?? 840, gpu.datacenter || false).
    sp["min_disk_gb"] = reqs.get("min_disk_gb") or 50
    min_dl = reqs.get("min_download_speed", 840)
    if min_dl > 0:
        sp["min_download_speed"] = min_dl
    else:
        sp.pop("min_download_speed", None)
    if not reqs.get("datacenter"):
        sp.pop("datacenter", None)
    verified = bool(reqs.get("verified", settings.get("verified_default", True)))
    sp["verified"] = True if verified else [True, False]
    sp["external"] = False if settings.get("exclude_external", True) else [True, False]
    sp["rentable"], sp["rented"] = True, False
    gpu_types = sp.pop("gpu_types", None) or []
    gpu_count = sp.pop("gpu_count", 1)
    if gpu_types:  # preferred_only defaults on when the card lists gpu_types
        sp["gpu_name"] = list(gpu_types)
    ctx = {"client_max": client_max, "min_dl": min_dl, "gpu_types": list(gpu_types),
           "gpu_count": gpu_count, "min_upload": reqs.get("min_upload_speed", 500),
           "limit": settings.get("broad_fetch_limit", 2000),
           "cap": settings.get("column_cap_broad", 100)}
    return sp, gate, ctx


def card_query(sp, limit):
    """Port of VastAIProvider.search_offers query-body construction."""
    q = {"type": "on-demand", "limit": limit, "external": {"eq": False},
         "rentable": {"eq": True}, "verified": {"eq": True}, "rented": {"eq": False}}
    kw = dict(sp)
    g = kw.pop("gpu_name", None)
    if g:
        q["gpu_name"] = {"in": list(g)} if isinstance(g, (list, tuple)) else {"eq": g}
    for key, field in _NAMED.items():
        v = kw.pop(key, None)
        if v:
            q[field] = {"gte": v}
    kw.pop("max_price", None)
    for key, v in kw.items():
        if v is None:
            continue
        if key in _CARD_MAP:
            field, op = _CARD_MAP[key]
            if key in _GB_TO_MB and isinstance(v, (int, float)):
                v = int(v * 1000)
            q[field] = {"in": v} if isinstance(v, list) else {op: v}
        elif key in _DIRECT:
            if isinstance(v, list):
                q[key] = {"in": v}
            elif isinstance(v, bool) or key in _EQ_DIRECT:
                q[key] = {"eq": v}
            else:
                q[key] = {"gte": v}
    return q


def _lacks_avx2(cpu):
    name = (cpu or "").replace("(R)", " ").replace("(TM)", " ").strip()
    if not name:
        return False
    if any(p.search(name) for p in _PRE_AVX2):
        return True
    m = _XEON_E.search(name)
    return bool(m and int(m.group(1) or 1) <= 2)


def _f(o, k, default=0.0):
    try:
        return float(o.get(k))
    except (TypeError, ValueError):
        return default


def card_filter(offers, gate, ctx, dash):
    """Client-side passes of search_offers + search_card_offers, in the dashboard's order."""
    deny = set(dash.get("denylist_machines") or [])
    blk_m = set((dash.get("blocklist") or {}).get("machines") or [])
    blk_h = set((dash.get("blocklist") or {}).get("hosts") or [])
    fav_m = set((dash.get("favorites") or {}).get("machines") or [])
    fav_h = set((dash.get("favorites") or {}).get("hosts") or [])
    fast = set(dash.get("fast_machines") or [])
    # provider: machine denylist, pre-AVX2 CPU screen
    pool = [o for o in offers if o.get("machine_id") not in deny and not _lacks_avx2(o.get("cpu_name"))]
    pool = [o for o in pool if o.get("rentable") is not False]
    if ctx["gpu_count"]:
        pool = [o for o in pool if o.get("num_gpus") == ctx["gpu_count"]]
    broad = pool
    if ctx["client_max"]:
        broad = [o for o in broad if _f(o, "dph_total") <= ctx["client_max"]]
    broad = [o for o in broad if _f(o, "inet_up") >= ctx["min_upload"] and _f(o, "inet_down") >= ctx["min_dl"]]
    broad.sort(key=lambda o: (o.get("gpu_name") or "", _f(o, "dph_total")))
    uncapped = len(broad)
    per, out = {}, []
    for o in broad:  # per-GPU column cap (settings.column_cap_broad)
        g = o.get("gpu_name")
        if per.get(g, 0) < ctx["cap"]:
            out.append(o)
            per[g] = per.get(g, 0) + 1
    seen_ids = {o.get("id") for o in out}
    seen_keys = {(o.get("machine_id"), o.get("num_gpus")) for o in out}

    def inject(pred):
        for o in pool:  # favorites / fast searches bypass max_price and the cap
            key = (o.get("machine_id"), o.get("num_gpus"))
            if pred(o) and o.get("id") not in seen_ids and key not in seen_keys:
                out.append(o)
                seen_ids.add(o.get("id"))
                seen_keys.add(key)

    inject(lambda o: o.get("machine_id") in fav_m)
    inject(lambda o: o.get("host_id") in fav_h)
    out = [o for o in out if o.get("machine_id") not in blk_m and o.get("host_id") not in blk_h]
    fast_ok = fast - blk_m
    inject(lambda o: o.get("machine_id") in fast_ok)
    if ctx["min_dl"] > 0:
        out = [o for o in out if _f(o, "inet_down") >= ctx["min_dl"]]
    # host-viability gate (missing fields are kept)
    min_cmg = gate.get("min_cuda_max_good")
    allowed = {str(g).strip().lower() for g in ctx["gpu_types"]}
    floors = [(mk, gate[k] * sc) for k, mk, sc in _FLOORS if gate.get(k)]

    def viable(o):
        if min_cmg is not None and o.get("cuda_max_good") is not None and _f(o, "cuda_max_good") < float(min_cmg):
            return False
        if gate.get("require_avx") is True and o.get("has_avx") is False:
            return False
        if allowed and o.get("gpu_name") is not None and str(o["gpu_name"]).strip().lower() not in allowed:
            return False
        return all(o.get(mk) is None or _f(o, mk, float("inf")) >= fl for mk, fl in floors)

    return [o for o in out if viable(o)], uncapped


def card_rows(ts, card, matched):
    name = card["name"]
    gpus = list((card.get("gpu_requirements") or {}).get("gpu_types") or [])
    for g in sorted({o.get("gpu_name") for o in matched if o.get("gpu_name")}):
        if g not in gpus:
            gpus.append(g)
    rows = []
    for g in gpus + ["ALL"]:
        sub = matched if g == "ALL" else [o for o in matched if o.get("gpu_name") == g]
        pairs = [(_f(o, "dph_total") / (int(o.get("num_gpus") or 1)), int(o.get("num_gpus") or 1))
                 for o in sub]
        row = {"ts_utc": ts, "gpu": g, "subset": f"card:{name}"}
        row.update(stats(pairs))
        rows.append(row)
    return rows


def log(line):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def main():
    _install_test_resolve()
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    has_key = _api_key() is not None
    if not has_key:
        log(f"{ts} WARN no API key (env VAST_API_KEY or {KEY_PATH}) - search capped at {UNAUTH_CAP} offers")

    results = {}
    raw_counts = {}
    cards, dash = load_cards()
    card_results = []  # [(card, matched_offers, uncapped_count, raw_count)]
    try:
        for gpu in GPUS:
            offers = fetch_offers(gpu)
            raw_counts[gpu] = len(offers)
            results[gpu] = normalise(offers)
        for card in cards:
            sp, gate, ctx = card_params(card, dash)
            offers = _search(card_query(sp, ctx["limit"]), f"card:{card['name']}")
            matched, uncapped = card_filter(offers, gate, ctx, dash)
            card_results.append((card, matched, uncapped, len(offers)))
    except DNSUnavailable:
        log(f"{ts} SKIP network/DNS unavailable - skipped")
        return 2
    except Exception as exc:  # noqa: BLE001
        log(f"{ts} FAIL {exc} - nothing written")
        return 2

    for card, _, _, raw in card_results:
        raw_counts[f"card:{card['name']}"] = raw
    if not has_key and any(n == UNAUTH_CAP for n in raw_counts.values()):
        counts = " ".join(f"{g.split()[-1]}={raw_counts[g]}" for g in GPUS)
        log(f"{ts} SKIP unauthenticated result capped at {UNAUTH_CAP} ({counts}) - nothing written")
        return 2

    rows = []
    for gpu in GPUS:
        offers = results[gpu]
        subsets = {"all": offers, "reliable": [o for o in offers if o["rel"] >= RELIABLE_MIN]}
        for name, subset in subsets.items():
            row = {"ts_utc": ts, "gpu": gpu, "subset": name}
            row.update(stats([(o["price"], o["num_gpus"]) for o in subset]))
            rows.append(row)
    for card, matched, _, _ in card_results:
        rows.extend(card_rows(ts, card, matched))

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
    cmed = "".join(
        f" | {c['name']}: " + " ".join(
            f"{r['gpu'].split()[-1]}={r['median'] if r['median'] != '' else '-'}(n={r['n_offers']})"
            for r in card_rows(ts, c, m))
        + f" [pool {raw}, pre-cap {unc}]"
        for c, m, unc, raw in card_results)
    log(f"{ts} OK median/gpu/hr {med}{cmed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
