"""Render data/prices.csv to chart.html (interactive, Chart.js) and chart.png (matplotlib)."""
import csv
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "prices.csv"
HTML_PATH = ROOT / "chart.html"
PNG_PATH = ROOT / "chart.png"

GPUS = ["RTX 3090", "RTX 4090", "RTX 5090"]
COLORS = {"RTX 3090": "#2f6fdf", "RTX 4090": "#e0741c", "RTX 5090": "#1f9d6b"}
FIELDS = ["median", "p25", "p75", "cheap5", "n_gpus", "n_offers"]


def parse_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)


def num(s):
    return float(s) if s not in ("", None) else None


def load():
    """-> {subset: {gpu: [[epoch_ms, median, p25, p75, cheap5, n_gpus, n_offers], ...]}}"""
    data = {"all": {g: [] for g in GPUS}, "reliable": {g: [] for g in GPUS}}
    if not CSV_PATH.exists():
        return data
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sub, gpu = row["subset"], row["gpu"]
            if sub not in data or gpu not in data[sub]:
                continue
            t = int(parse_ts(row["ts_utc"]).timestamp() * 1000)
            data[sub][gpu].append([t] + [num(row[k]) for k in FIELDS])
    for sub in data.values():
        for series in sub.values():
            series.sort(key=lambda r: r[0])
    return data


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vast.ai GPU prices</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
<style>
:root {
  color-scheme: light dark;
  --bg: #ffffff; --panel: #f6f7f9; --fg: #1b1f24; --muted: #5d6673;
  --grid: rgba(0,0,0,0.08); --border: #d9dde3; --accent: #2f6fdf;
  --up: #c0392b; --down: #1f9d6b;
  --c3090: #2f6fdf; --c4090: #e0741c; --c5090: #1f9d6b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1216; --panel: #181c22; --fg: #e6e9ee; --muted: #98a2b0;
    --grid: rgba(255,255,255,0.08); --border: #2a3039; --accent: #6ea0ff;
    --up: #ff7b6b; --down: #4fd39e;
    --c3090: #6ea0ff; --c4090: #ff9d4d; --c5090: #4fd39e;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 12px; }
h1 { font-size: 18px; margin: 4px 0 2px; }
.sub { color: var(--muted); font-size: 12px; margin-bottom: 10px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.group { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.group button { background: var(--panel); color: var(--fg); border: 0; padding: 6px 12px;
  font: inherit; cursor: pointer; border-right: 1px solid var(--border); }
.group button:last-child { border-right: 0; }
.group button.on { background: var(--accent); color: #fff; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; margin-bottom: 10px; }
.stat { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.stat .name { font-weight: 600; display: flex; align-items: center; gap: 6px; }
.stat .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.stat .val { font-size: 20px; font-variant-numeric: tabular-nums; }
.stat .chg { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.up { color: var(--up); } .down { color: var(--down); }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 8px; margin-bottom: 10px; }
.panel h2 { font-size: 13px; font-weight: 600; margin: 0 0 4px; color: var(--muted); }
.price { position: relative; height: 360px; }
.supply { position: relative; height: 200px; }
@media (max-width: 600px) { .price { height: 280px; } .supply { height: 170px; } }
</style>
</head>
<body>
<main>
  <h1>Vast.ai on-demand GPU prices ($/GPU/hr)</h1>
  <div class="sub" id="updated"></div>
  <div class="controls">
    <div class="group" id="subset">
      <button data-v="all" class="on">All offers</button>
      <button data-v="reliable">Reliable &ge;0.98</button>
    </div>
    <div class="group" id="range">
      <button data-v="24">24h</button>
      <button data-v="168" class="on">7d</button>
      <button data-v="720">30d</button>
      <button data-v="0">All</button>
    </div>
  </div>
  <div class="stats" id="stats"></div>
  <div class="panel"><h2>Median per-GPU price (band p25&ndash;p75, dashed = mean of 5 cheapest)</h2>
    <div class="price"><canvas id="price"></canvas></div></div>
  <div class="panel"><h2>Supply: rentable GPUs listed</h2>
    <div class="supply"><canvas id="supply"></canvas></div></div>
</main>
<script>
const DATA = __DATA__;
const GPUS = __GPUS__;
const H = 3600e3;
let subset = "all", rangeH = 168, priceChart = null, supplyChart = null;

const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const colorOf = g => css("--c" + g.split(" ").pop());
function alpha(hex, a) {
  const h = hex.replace("#", "");
  const v = parseInt(h.length === 3 ? h.split("").map(c => c + c).join("") : h, 16);
  return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${a})`;
}
// row: [t, median, p25, p75, cheap5, n_gpus, n_offers]
const pts = (rows, i) => rows.filter(r => r[i] !== null).map(r => ({x: r[0], y: r[i]}));

function latestT() {
  let t = 0;
  for (const s of Object.values(DATA)) for (const rows of Object.values(s))
    for (const r of rows) t = Math.max(t, r[0]);
  return t;
}
function firstT() {
  let t = Infinity;
  for (const s of Object.values(DATA)) for (const rows of Object.values(s))
    for (const r of rows) t = Math.min(t, r[0]);
  return t;
}
// x window: the chosen range, but never wider than the data actually collected
function xWindow() {
  const end = latestT(), start = firstT(), span = end - start;
  const pad = Math.max(span * 0.04, 10 * 60e3);
  const min = rangeH && span > rangeH * H ? end - rangeH * H : start - pad;
  return {min, max: end + pad};
}
function nearest(rows, target, tolH) {
  let best = null;
  for (const r of rows) if (r[1] !== null && Math.abs(r[0] - target) <= tolH * H &&
      (!best || Math.abs(r[0] - target) < Math.abs(best[0] - target))) best = r;
  return best;
}
function chg(cur, old, label) {
  if (!old) return `<span>${label}: n/a</span>`;
  const d = cur - old[1], pct = old[1] ? 100 * d / old[1] : 0;
  const cls = d > 0 ? "up" : d < 0 ? "down" : "";
  const sign = d > 0 ? "+" : "";
  return `<span class="${cls}">${label}: ${sign}${d.toFixed(3)} (${sign}${pct.toFixed(1)}%)</span>`;
}

function renderStats() {
  const el = document.getElementById("stats");
  el.innerHTML = GPUS.map(g => {
    const rows = DATA[subset][g];
    const last = [...rows].reverse().find(r => r[1] !== null);
    if (!last) return `<div class="stat"><div class="name">${g}</div><div class="val">&ndash;</div></div>`;
    const d1 = nearest(rows, last[0] - 24 * H, 3), d7 = nearest(rows, last[0] - 168 * H, 12);
    return `<div class="stat"><div class="name"><span class="dot" style="background:${colorOf(g)}"></span>${g}</div>
      <div class="val">$${last[1].toFixed(3)}</div>
      <div class="chg">cheap5 $${last[4] !== null ? last[4].toFixed(3) : "-"} &middot; ${last[5]} GPUs / ${last[6]} offers</div>
      <div class="chg">${chg(last[1], d1, "vs 24h")} &middot; ${chg(last[1], d7, "vs 7d")}</div></div>`;
  }).join("");
}

function baseOpts(yTitle, fmt) {
  const grid = css("--grid"), fg = css("--muted");
  const xw = xWindow();
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: {mode: "nearest", axis: "x", intersect: false},
    scales: {
      x: {type: "time", min: xw.min, max: xw.max,
          grid: {color: grid}, ticks: {color: fg, maxRotation: 0, autoSkipPadding: 16}},
      y: {grid: {color: grid}, ticks: {color: fg, callback: fmt}, title: {display: true, text: yTitle, color: fg}}
    },
    plugins: {
      legend: {labels: {color: css("--fg"), boxWidth: 12, filter: i => !i.text.startsWith("_")}},
      tooltip: {filter: i => !i.dataset.label.startsWith("_")}
    }
  };
}

function render() {
  const n = Math.max(...GPUS.map(g => DATA[subset][g].length), 0);
  const r = n <= 60 ? 3 : 0;
  const pDs = [], sDs = [];
  for (const g of GPUS) {
    const rows = DATA[subset][g], c = colorOf(g);
    pDs.push({label: "_p25 " + g, data: pts(rows, 2), borderWidth: 0, pointRadius: 0, fill: false, borderColor: "transparent"});
    pDs.push({label: "_p75 " + g, data: pts(rows, 3), borderWidth: 0, pointRadius: 0, fill: "-1",
              borderColor: "transparent", backgroundColor: alpha(c, 0.15)});
    pDs.push({label: g + " median", data: pts(rows, 1), borderColor: c, backgroundColor: c,
              borderWidth: 2, pointRadius: r, pointHoverRadius: 5, fill: false, tension: 0.2});
    pDs.push({label: g + " cheap5", data: pts(rows, 4), borderColor: c, backgroundColor: c,
              borderWidth: 1.5, borderDash: [5, 4], pointRadius: r ? 2 : 0, fill: false, tension: 0.2});
    sDs.push({label: g, data: pts(rows, 5), borderColor: c, backgroundColor: c,
              borderWidth: 2, pointRadius: r, fill: false, tension: 0.2});
  }
  if (priceChart) priceChart.destroy();
  if (supplyChart) supplyChart.destroy();
  priceChart = new Chart(document.getElementById("price"),
    {type: "line", data: {datasets: pDs}, options: baseOpts("$/GPU/hr", v => "$" + Number(v).toFixed(2))});
  supplyChart = new Chart(document.getElementById("supply"),
    {type: "line", data: {datasets: sDs}, options: baseOpts("GPUs", v => v)});
  renderStats();
}

function wire(id, set) {
  document.querySelectorAll(`#${id} button`).forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(`#${id} button`).forEach(x => x.classList.toggle("on", x === b));
    set(b.dataset.v); render();
  }));
}
wire("subset", v => subset = v);
wire("range", v => rangeH = Number(v));
const lt = latestT();
document.getElementById("updated").textContent = lt
  ? "Latest snapshot: " + new Date(lt).toISOString().slice(0, 16).replace("T", " ") + " UTC  (generated __GEN__)"
  : "No data yet.";
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
render();
</script>
</body>
</html>
"""


def write_html(data):
    gen = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = (HTML.replace("__DATA__", json.dumps(data, separators=(",", ":")))
                .replace("__GPUS__", json.dumps(GPUS))
                .replace("__GEN__", gen))
    HTML_PATH.write_text(html, encoding="utf-8")


def write_png(data):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1]})
    sub = data["all"]
    n = max((len(sub[g]) for g in GPUS), default=0)
    marker = "o" if n <= 60 else None
    for g in GPUS:
        rows = [r for r in sub[g] if r[1] is not None]
        if not rows:
            continue
        c = COLORS[g]
        t = [dt.datetime.fromtimestamp(r[0] / 1000, dt.timezone.utc) for r in rows]
        ax1.fill_between(t, [r[2] for r in rows], [r[3] for r in rows], color=c, alpha=0.15, lw=0)
        if len(rows) == 1:  # a band of one point is invisible; show it as an error bar
            ax1.errorbar(t, [rows[0][1]], yerr=[[rows[0][1] - rows[0][2]], [rows[0][3] - rows[0][1]]],
                         color=c, alpha=0.4, capsize=6, lw=6)
        ax1.plot(t, [r[1] for r in rows], color=c, lw=2, marker=marker, ms=4,
                 label=f"{g} median ${rows[-1][1]:.3f}")
        ax1.plot(t, [r[4] for r in rows], color=c, lw=1.2, ls="--", marker=marker, ms=3,
                 label=f"{g} cheap5")
        ax2.plot(t, [r[5] for r in rows], color=c, lw=2, marker=marker, ms=4,
                 label=f"{g} ({int(rows[-1][5])} GPUs)")
    ax1.set_ylabel("$/GPU/hr")
    ax1.set_title("Vast.ai on-demand per-GPU price (all offers) - median, p25-p75 band, cheap5 dashed",
                  fontsize=10)
    ax2.set_ylabel("GPUs listed")
    ax2.set_title("Supply", fontsize=10)
    for ax in (ax1, ax2):
        ax.grid(alpha=0.3)
        if ax.has_data():
            ax.legend(fontsize=8, ncol=3, loc="best", framealpha=0.85)
    ts = sorted({r[0] for g in GPUS for r in sub[g]})
    if len(ts) == 1:  # single snapshot: matplotlib would auto-span years; pad +-1h instead
        t0 = dt.datetime.fromtimestamp(ts[0] / 1000, dt.timezone.utc)
        ax2.set_xlim(t0 - dt.timedelta(hours=1), t0 + dt.timedelta(hours=1))
    loc = mdates.AutoDateLocator(tz=dt.timezone.utc)
    ax2.xaxis.set_major_locator(loc)
    ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc, tz=dt.timezone.utc))
    ax2.set_xlabel("UTC")
    if n == 0:
        ax1.text(0.5, 0.5, "No data yet", transform=ax1.transAxes, ha="center")
    fig.tight_layout()
    fig.savefig(PNG_PATH, dpi=110)
    plt.close(fig)


def main():
    data = load()
    write_html(data)
    write_png(data)
    rows = sum(len(v) for v in data["all"].values())
    print(f"chart: {rows} 'all' points -> {HTML_PATH.name}, {PNG_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
