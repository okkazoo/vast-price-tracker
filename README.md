# vast-price-tracker

Hourly snapshot of Vast.ai **on-demand, rentable** offers for RTX 3090, RTX 4090 and RTX 5090.
Prices are **per GPU** (`dph_total / num_gpus`). Each run stores, per GPU and per subset
(`all` offers, `reliable` = reliability2 >= 0.98): offer count, GPU count (supply), min, p10, p25,
median, p75, p90, mean, `cheap5` (mean of the 5 cheapest), and a GPU-weighted median.

![chart](chart.png)

## Files

| File | What |
|---|---|
| `collect.py` | Queries the public Vast search API (stdlib only), appends 6 rows to `data/prices.csv`, logs one line to `data/collect.log`, keeps a raw per-offer snapshot in `data/raw/` (local only, gitignored). All-or-nothing: if any GPU query fails, nothing is written and it exits 2. |
| `chart.py` | Renders `chart.html` (interactive) and `chart.png` (static, `all` subset). |
| `run.cmd` | Runs collect then chart; output appended to `data/run.log`. |
| `install_task.ps1` / `uninstall_task.ps1` | Windows Scheduled Task on/off. |
| `.github/workflows/collect.yml` | Same job on GitHub Actions. |

## Viewing

Open `chart.html` in a browser. Toggle all/reliable, pick 24h / 7d / 30d / all. The top panel is
median $/GPU/hr with a p25-p75 band and a dashed cheap5 line; the bottom panel is supply (GPUs listed).
The page loads Chart.js from cdn.jsdelivr.net, so it needs internet (see the VPN note below).
`chart.png` is the offline/phone view.

## Local schedule (Windows)

```
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

Registers `VastPriceTracker`: hourly at :17, current user, runs on battery, catches up after sleep
(StartWhenAvailable), no console window (`conhost --headless`). Re-running it replaces the task.
Remove with `uninstall_task.ps1`. Needs `python` (with matplotlib) on PATH.

**VPN gap:** on the GlobalProtect VPN, DNS cannot resolve `console.vast.ai`. Those runs log
`network/DNS unavailable - skipped`, exit 2 and write no row, so the series simply has holes
while the VPN is up (and while the laptop is off). The charts still re-render.

## Switching to GitHub Actions (no gaps)

1. Create a **private** GitHub repo and push this one to it. No secrets are needed; the API is public.
2. The workflow runs hourly at :17 UTC (plus a manual "Run workflow" button), and commits
   `data/prices.csv`, `data/collect.log`, `chart.html` and `chart.png` when they change.
3. Uninstall the local task (`uninstall_task.ps1`) so the two do not both write to `prices.csv`,
   and `git pull` to get the latest chart.

GitHub's scheduled runs can be delayed by several minutes. Each run takes under a minute, so a
private repo uses roughly 720 of the free 2,000 Actions minutes per month.

## Test-only override

`VPT_RESOLVE=<ip>` makes `console.vast.ai` resolve to that IP (TLS still uses the real hostname).
It is only for manual testing on a broken resolver and is never set by the scheduled runs.
