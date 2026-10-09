"""Collecte multi-modèles et calcul des scores par spot.

Lancé par GitHub Actions plusieurs fois par jour. Produit :
- docs/data/latest.json : ce que lit l'app sur le téléphone
- data/archive/AAAA/MM/JJ_HHMM.json.gz : toutes les prévisions brutes, pour
  comparer plus tard modèles / bouées / sessions réelles (calibration)
"""

from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
SPOTS_FILE = ROOT / "config" / "spots.json"
LATEST_FILE = ROOT / "docs" / "data" / "latest.json"
ARCHIVE_DIR = ROOT / "data" / "archive"

TZ = "Europe/Paris"
FORECAST_DAYS = 6

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WIND_URL = "https://api.open-meteo.com/v1/forecast"

# Codes Open-Meteo. Un modèle indisponible est simplement ignoré (et signalé
# dans latest.json), il ne casse pas la collecte.
WAVE_MODELS = {
    "meteofrance_wave": "Météo-France MFWAM",
    "ecmwf_wam": "ECMWF WAM 9 km",
    "ecmwf_wam025": "ECMWF WAM 0.25°",
    "ncep_gfswave016": "GFS Wave (WW3) 0.16°",
    "ncep_gfswave025": "GFS Wave (WW3) 0.25°",
    "dwd_ewam": "DWD EWAM (Europe)",
    "dwd_gwam": "DWD GWAM",
}
WIND_MODELS = {
    "meteofrance_arome_france": "Arome",
    "meteofrance_arpege_europe": "Arpège",
    "ecmwf_ifs025": "ECMWF IFS",
    "gfs_seamless": "GFS",
    "icon_seamless": "ICON (DWD)",
}

WAVE_VARS = [
    "wave_height", "wave_period", "wave_peak_period", "wave_direction",
    "swell_wave_height", "swell_wave_period", "swell_wave_direction",
    "wind_wave_height", "wind_wave_period", "wind_wave_direction",
]
WIND_VARS = ["wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"]


# --------------------------------------------------------------------------
# Collecte
# --------------------------------------------------------------------------

def get_json(url: str, params: dict, tries: int = 3) -> dict | None:
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 429:
                print(f"  ! limite d'appels atteinte ({params.get('models')}), pause")
                time.sleep(20)
                continue
            if r.status_code == 400:
                print(f"  ! refusé ({params.get('models')}): {r.text[:200]}")
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"  ! tentative {attempt + 1} échouée ({params.get('models')}): {e}")
            time.sleep(3 * (attempt + 1))
    return None


def fetch_model(url: str, spots: list[dict], model: str, variables: list[str], marine: bool) -> list[dict | None]:
    """Un seul appel par modèle pour tous les spots (moins d'appels, moins de refus)."""
    params = {
        "latitude": ",".join(str(s["lat"]) for s in spots),
        "longitude": ",".join(str(s["lon"]) for s in spots),
        "hourly": ",".join(variables),
        "models": model,
        "timezone": TZ,
        "forecast_days": FORECAST_DAYS,
    }
    if marine:
        params["cell_selection"] = "sea"
    data = get_json(url, params)
    if data is None:
        return [None] * len(spots)
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        hourly = (d or {}).get("hourly")
        if not hourly or not any(v is not None for v in hourly.get(variables[0], [])):
            out.append(None)
            continue
        hourly["_grid"] = {"lat": d.get("latitude"), "lon": d.get("longitude")}
        out.append(hourly)
    return out + [None] * (len(spots) - len(out))


# --------------------------------------------------------------------------
# Outils angulaires
# --------------------------------------------------------------------------

def angle_in_window(angle: float, lo: float, hi: float) -> bool:
    angle %= 360
    if lo <= hi:
        return lo <= angle <= hi
    return angle >= lo or angle <= hi  # fenêtre qui passe par le nord


def angular_distance_to_window(angle: float, lo: float, hi: float) -> float:
    if angle_in_window(angle, lo, hi):
        return 0.0
    d1 = min(abs(angle - lo) % 360, 360 - abs(angle - lo) % 360)
    d2 = min(abs(angle - hi) % 360, 360 - abs(angle - hi) % 360)
    return min(d1, d2)


def circular_mean(angles: list[float]) -> float | None:
    if not angles:
        return None
    s = sum(math.sin(math.radians(a)) for a in angles)
    c = sum(math.cos(math.radians(a)) for a in angles)
    return (math.degrees(math.atan2(s, c)) + 360) % 360


# --------------------------------------------------------------------------
# Modèle de "taille surfable" — volontairement simple, à calibrer
# --------------------------------------------------------------------------

def period_factor(t: float) -> float:
    """En Méditerranée la période fait tout : 5 s ne donne presque rien."""
    if t < 4:
        return 0.3
    if t < 5:
        return 0.5
    if t < 6:
        return 0.65
    if t < 7:
        return 0.8
    if t < 8:
        return 0.9
    if t < 10:
        return 1.0
    return 1.1


def surf_height(hs, period, direction, spot) -> float | None:
    """Estimation de la taille au spot à partir de la houle au large."""
    if hs is None or period is None or direction is None:
        return None
    w = spot["swell_window"]
    off = angular_distance_to_window(direction, w["min"], w["max"])
    dir_factor = max(0.0, 1 - off / 35)  # on perd tout à 35° hors fenêtre
    if period < spot.get("min_period", 0) - 1:
        return round(hs * 0.2 * dir_factor, 2)
    return round(hs * period_factor(period) * dir_factor, 2)


def wind_quality(speed_kmh, direction, spot) -> str | None:
    if speed_kmh is None or direction is None:
        return None
    w = spot["offshore_wind"]
    offshore = angle_in_window(direction, w["min"], w["max"])
    if speed_kmh < 10:
        return "glassy"
    if offshore:
        return "offshore" if speed_kmh < 35 else "offshore_fort"
    side = angular_distance_to_window(direction, w["min"], w["max"]) < 60
    if speed_kmh < 18:
        return "faible"
    if side and speed_kmh < 25:
        return "side"
    return "onshore"


WIND_PENALTY = {"glassy": 0, "offshore": 0, "offshore_fort": 0.5, "faible": 0.25,
                "side": 0.75, "onshore": 2, None: 0}


def rating(h: float | None, wq: str | None) -> int:
    if h is None:
        return 0
    if h < 0.35:
        base = 0
    elif h < 0.6:
        base = 1
    elif h < 1.0:
        base = 2
    elif h < 1.6:
        base = 3
    else:
        base = 4
    return int(max(0, round(base - WIND_PENALTY.get(wq, 0))))


def confidence(values: list[float]) -> str:
    """Accord entre modèles : c'est l'indicateur clé à J+3/J+5."""
    if len(values) < 2:
        return "faible"
    med = statistics.median(values)
    spread = max(values) - min(values)
    if len(values) >= 3 and spread <= 0.25 * med + 0.15:
        return "forte"
    if spread <= 0.6 * med + 0.2:
        return "moyenne"
    return "faible"


# --------------------------------------------------------------------------
# Traitement d'un spot
# --------------------------------------------------------------------------

def process_spot(spot: dict, raw_waves: dict, raw_wind: dict, now_local: datetime) -> dict:
    times = None
    for series in list(raw_waves.values()) + list(raw_wind.values()):
        times = series["time"]
        break
    if times is None:
        return {"id": spot["id"], "name": spot["name"], "area": spot.get("area"),
                "error": "aucun modèle disponible", "hourly": [], "days": []}

    per_model_height: dict[str, list] = {m: [] for m in raw_waves}
    hourly_out = []

    for i, t in enumerate(times):
        estimates, periods, dirs, hs_list = [], [], [], []
        for model, s in raw_waves.items():
            hs = s["wave_height"][i] if i < len(s["wave_height"]) else None
            period = (s.get("wave_peak_period") or [None] * len(times))[i] or s["wave_period"][i]
            direction = s["wave_direction"][i]
            est = surf_height(hs, period, direction, spot)
            per_model_height[model].append(est)
            if est is not None:
                estimates.append(est)
                periods.append(period)
                dirs.append(direction)
                hs_list.append(hs)

        speeds, wdirs, gusts = [], [], []
        for model, s in raw_wind.items():
            sp = s["wind_speed_10m"][i] if i < len(s["wind_speed_10m"]) else None
            wd = s["wind_direction_10m"][i] if i < len(s["wind_direction_10m"]) else None
            g = (s.get("wind_gusts_10m") or [None] * len(times))[i]
            if sp is not None and wd is not None:
                speeds.append(sp)
                wdirs.append(wd)
            if g is not None:
                gusts.append(g)

        h_med = round(statistics.median(estimates), 2) if estimates else None
        wind = round(statistics.median(speeds)) if speeds else None
        wdir = circular_mean(wdirs)
        wq = wind_quality(wind, wdir, spot)
        hourly_out.append({
            "t": t,
            "h": h_med,
            "h_min": min(estimates) if estimates else None,
            "h_max": max(estimates) if estimates else None,
            "hs": round(statistics.median(hs_list), 2) if hs_list else None,
            "T": round(statistics.median(periods), 1) if periods else None,
            "dir": round(circular_mean(dirs)) if dirs else None,
            "wind": wind,
            "gust": round(statistics.median(gusts)) if gusts else None,
            "wdir": round(wdir) if wdir is not None else None,
            "wq": wq,
            "rating": rating(h_med, wq),
            "conf": confidence(estimates) if estimates else None,
            "n": len(estimates),
        })

    days = summarize_days(hourly_out, now_local)
    return {
        "id": spot["id"],
        "name": spot["name"],
        "area": spot.get("area"),
        "swell_window": spot["swell_window"],
        "offshore_wind": spot["offshore_wind"],
        "models": {
            "waves": [WAVE_MODELS[m] for m in raw_waves],
            "wind": [WIND_MODELS[m] for m in raw_wind],
        },
        "per_model": {WAVE_MODELS[m]: v for m, v in per_model_height.items()},
        "hourly": hourly_out,
        "days": days,
    }


def summarize_days(hourly: list[dict], now_local: datetime) -> list[dict]:
    by_day: dict[str, list[dict]] = {}
    for h in hourly:
        by_day.setdefault(h["t"][:10], []).append(h)

    out = []
    for day, hours in sorted(by_day.items()):
        daylight = [h for h in hours if 7 <= int(h["t"][11:13]) <= 19]
        future = [h for h in daylight if h["t"] >= now_local.strftime("%Y-%m-%dT%H:00")]
        pool = future if day == now_local.strftime("%Y-%m-%d") else daylight
        if not pool:
            continue
        best = max(pool, key=lambda h: (h["rating"], h["h"] or 0))
        # plus longue fenêtre continue avec une note >= 1
        window, cur = [], []
        for h in pool:
            if h["rating"] >= 1:
                cur.append(h)
                if len(cur) > len(window):
                    window = list(cur)
            else:
                cur = []
        out.append({
            "date": day,
            "rating": best["rating"],
            "h": best["h"],
            "h_min": best["h_min"],
            "h_max": best["h_max"],
            "T": best["T"],
            "dir": best["dir"],
            "wind": best["wind"],
            "wdir": best["wdir"],
            "wq": best["wq"],
            "conf": best["conf"],
            "best_hour": best["t"][11:16],
            "window": [window[0]["t"][11:16], window[-1]["t"][11:16]] if window else None,
        })
    return out


def add_trend(spots_out: list[dict], previous: dict | None) -> None:
    """Évolution par rapport au run précédent : une prévision qui se confirme
    run après run est plus fiable qu'une qui saute."""
    if not previous:
        return
    prev_days = {}
    for s in previous.get("spots", []):
        for d in s.get("days", []):
            prev_days[(s["id"], d["date"])] = d
    for s in spots_out:
        for d in s["days"]:
            p = prev_days.get((s["id"], d["date"]))
            if not p or p.get("h") is None or d.get("h") is None:
                continue
            delta = d["h"] - p["h"]
            d["trend"] = "hausse" if delta > 0.15 else "baisse" if delta < -0.15 else "stable"
            d["prev_h"] = p["h"]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    spots = json.loads(SPOTS_FILE.read_text(encoding="utf-8"))["spots"]
    run_utc = datetime.now(timezone.utc)
    now_local = run_utc.astimezone(ZoneInfo(TZ))

    previous = None
    if LATEST_FILE.exists():
        try:
            previous = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = None

    archive = {"run_utc": run_utc.isoformat(), "spots": {}}
    spots_out = []
    failures = set()

    waves_by_spot = [{} for _ in spots]
    wind_by_spot = [{} for _ in spots]
    for model in WAVE_MODELS:
        print(f"> vagues {model}")
        for i, series in enumerate(fetch_model(MARINE_URL, spots, model, WAVE_VARS, marine=True)):
            if series:
                waves_by_spot[i][model] = series
        if not any(model in w for w in waves_by_spot):
            failures.add(model)
        time.sleep(1)
    for model in WIND_MODELS:
        print(f"> vent {model}")
        for i, series in enumerate(fetch_model(WIND_URL, spots, model, WIND_VARS, marine=False)):
            if series:
                wind_by_spot[i][model] = series
        if not any(model in w for w in wind_by_spot):
            failures.add(model)
        time.sleep(1)

    for spot, raw_waves, raw_wind in zip(spots, waves_by_spot, wind_by_spot):
        print(f"  {spot['name']} : {len(raw_waves)} modèles de vagues, {len(raw_wind)} de vent")
        archive["spots"][spot["id"]] = {"waves": raw_waves, "wind": raw_wind}
        spots_out.append(process_spot(spot, raw_waves, raw_wind, now_local))

    if not any(s["hourly"] for s in spots_out):
        print("Aucune donnée récupérée, latest.json n'est pas modifié.")
        return 1

    add_trend(spots_out, previous)

    latest = {
        "generated_at": run_utc.isoformat(),
        "generated_local": now_local.strftime("%d/%m %H:%M"),
        "unavailable_models": sorted(
            WAVE_MODELS.get(m) or WIND_MODELS.get(m) for m in failures
        ),
        "spots": spots_out,
    }
    LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    LATEST_FILE.write_text(json.dumps(latest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    path = ARCHIVE_DIR / run_utc.strftime("%Y/%m") / f"{run_utc.strftime('%d_%H%M')}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(archive, f, separators=(",", ":"))
    print(f"OK : {LATEST_FILE.relative_to(ROOT)} et {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
