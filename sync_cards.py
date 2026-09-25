"""Refresh cards.json from the local card.yml files and the dashboard's local state.

LOCAL ONLY (needs PyYAML and the two sibling repos on this laptop). collect.py on
GitHub Actions reads cards.json and never touches these paths.

    python sync_cards.py                      # refresh every name in cards.json "tracked"
    python sync_cards.py <card> [<card> ...]  # add/refresh these cards (appended to "tracked")

What it copies, and why: the dashboard's Offers / Offers-Auto search for a card
(vastai-api-manager dashboard/backend/routes/cards.py search_card_offers) reads
more than the card.yml. Everything below lives only on the laptop, so it is
snapshotted here for Actions to apply:
  - card gpu_requirements                (comfyui-projects/*/cards/<name>/card.yml)
  - box-profile host gates               (data/box_profiles.json, active profile or the
                                          card's provisioning.box_profile override)
  - settings verified_default / exclude_external / broad_fetch_limit / column_cap_broad
                                         (data/settings.json)
  - blocklist machines + hosts           (data/blocklist.json)  -> dropped
  - favorites machines + hosts           (data/favorites.json)  -> bypass max_price
  - fast machines                        (data/fast_machines.json) -> bypass max_price
  - provider machine denylist            (_BLOCKED_MACHINES in src/vastai_manager/providers/vastai.py)
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CARDS_JSON = ROOT / "cards.json"
PROJECTS = Path("C:/Users/craig/Documents/Dev/comfyui-projects")
DASH = Path("C:/Users/craig/Documents/Dev/vastai-api-manager")


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def find_card(name):
    hits = sorted(PROJECTS.glob(f"*/cards/{name}/card.yml"))
    if not hits:
        raise SystemExit(f"card not found: {PROJECTS}/*/cards/{name}/card.yml")
    if len(hits) > 1:
        raise SystemExit(f"card name is ambiguous: {[str(h) for h in hits]}")
    return hits[0]


def box_profile_gates(override):
    """Port of core/box_profiles.search_filters()."""
    data = _read_json(DASH / "data" / "box_profiles.json", {})
    profiles = data.get("profiles", {})
    key = override if (override and override in profiles) else data.get("active")
    if key not in profiles:
        return None, {}
    req = profiles[key].get("requires", {}) or {}
    out = {}
    if req.get("cuda_max_good") is not None:
        out["min_cuda_max_good"] = req["cuda_max_good"]
    if req.get("has_avx") is True:
        out["require_avx"] = True
    return key, out


def provider_denylist():
    src = (DASH / "src" / "vastai_manager" / "providers" / "vastai.py").read_text(encoding="utf-8")
    m = re.search(r"_BLOCKED_MACHINES[^=]*=\s*\{(.*?)\n\}", src, re.S)
    return sorted(int(x) for x in re.findall(r"^\s*(\d+)\s*:", m.group(1), re.M)) if m else []


def card_entry(name):
    path = find_card(name)
    card = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    override = (card.get("provisioning") or {}).get("box_profile")
    prof_key, gates = box_profile_gates(override)
    reqs = card.get("gpu_requirements") or {}
    if reqs.get("min_cpu_features"):
        print(f"WARN {name}: min_cpu_features={reqs['min_cpu_features']} is not mirrored by collect.py")
    return {
        "name": name,
        "card_path": path.relative_to(PROJECTS).as_posix(),
        "version": card.get("version"),
        "gpu_requirements": reqs,
        "box_profile": prof_key,
        "box_profile_gates": gates,
    }


def dashboard_state():
    settings = _read_json(DASH / "data" / "settings.json", {})
    block = _read_json(DASH / "data" / "blocklist.json", {})
    favs = _read_json(DASH / "data" / "favorites.json", {})
    fast = _read_json(DASH / "data" / "fast_machines.json", {})
    ids = lambda d: sorted(int(k) for k in (d or {}).keys())  # noqa: E731
    return {
        "settings": {
            "verified_default": settings.get("verified_default", True),
            "exclude_external": settings.get("exclude_external", True),
            "broad_fetch_limit": settings.get("broad_fetch_limit", 2000),
            "column_cap_broad": settings.get("column_cap_broad", 100),
        },
        "blocklist": {"machines": ids(block.get("machines")), "hosts": ids(block.get("hosts"))},
        "favorites": {"machines": ids(favs.get("machines")), "hosts": ids(favs.get("hosts"))},
        "fast_machines": ids(fast.get("machines")),
        "denylist_machines": provider_denylist(),
    }


def main(argv):
    doc = _read_json(CARDS_JSON, {})
    tracked = list(doc.get("tracked") or [])
    for n in argv:
        if n not in tracked:
            tracked.append(n)
    if not tracked:
        raise SystemExit("nothing to sync: pass card names or add them to cards.json 'tracked'")
    out = {
        "_doc": "Generated by sync_cards.py (local). collect.py reads it on Actions. Edit 'tracked' "
                "and re-run sync_cards.py; do not hand-edit the rest.",
        "synced_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "tracked": tracked,
        "cards": [card_entry(n) for n in tracked],
        "dashboard": dashboard_state(),
    }
    CARDS_JSON.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    d = out["dashboard"]
    print(f"wrote {CARDS_JSON.name}: {len(tracked)} card(s) {tracked}; blocklist "
          f"{len(d['blocklist']['machines'])}m/{len(d['blocklist']['hosts'])}h, favorites "
          f"{len(d['favorites']['machines'])}m/{len(d['favorites']['hosts'])}h, fast "
          f"{len(d['fast_machines'])}, denylist {d['denylist_machines']}")


if __name__ == "__main__":
    main(sys.argv[1:])
