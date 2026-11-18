"""gta6-twitch-ccu-forecast-01: improved model using comparable game launches.

Beats the persistence baseline by:
1. Using comparable game launch data to model hour-1-to-hour-N decay patterns
2. Weighting comparables by similarity (same platform, same genre, same audience)
3. Combining with GTA VI's own pre-launch history trend
4. Learning spread from comparable launch variance rather than fixed 0.15 sigma

The model:
- For each comparable launch, compute hour-h-to-hour-1 ratio at each quantile level
- Weight comparables by closeness to GTA (AAA, open-world, multi-platform, high CCU)
- Blend with GTA VI's own pre-launch hourly pattern
- Output calibrated quantiles
"""

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]

# Standard normal quantiles for each level
Z = [-1.645, -0.674, 0, 0.674, 1.645]

# GTA VI launch characteristics for comparable weighting
GTA6_META = {
    "platforms": ["pc", "playstation", "xbox"],
    "genre": "open-world action",
    "hype_level": "extreme",  # record-breaking anticipation
    "franchise_power": "top-tier",
    "pre_order_millions": 25,
    "trailer_views_billions": 3.5,
    "twitch_followers_millions": 45,
}

# Comparable game scores (pre-computed similarity to GTA VI)
COMPARABLE_SCORES = {
    # High similarity
    "Cyberpunk 2077": 0.85, "Red Dead Redemption 2": 0.82,
    "Hogwarts Legacy": 0.72, "Elden Ring": 0.75,
    "Starfield": 0.68, "Baldur's Gate 3": 0.62,
    "Call of Duty: Modern Warfare III": 0.58, "Diablo IV": 0.55,
    "Counter-Strike 2": 0.35, "Valorant": 0.30,
    "Apex Legends": 0.28, "Fortnite Chapter 5": 0.25,
    "Destiny 2: The Final Shape": 0.55, "Final Fantasy XVI": 0.60,
    "The Legend of Zelda: Tears of the Kingdom": 0.70,
    "Spider-Man 2": 0.58, "Assassin's Creed Shadows": 0.45,
    "Dragon's Dogma 2": 0.40, "Palworld": 0.32,
    "Helldivers 2": 0.35, "Call of Duty: Black Ops 6": 0.55,
}


def round2(x: float) -> float | int:
    """Round to 2 decimals, half up. Returns int when result is whole."""
    scaled = x * 100
    whole = math.floor(scaled)
    result = (whole + 1) / 100 if scaled - whole >= 0.5 else whole / 100
    return int(result) if result == int(result) else result


def compute_comparable_ratios(comparables: list[dict], game_name: str = "Grand Theft Auto VI") -> dict[int, list[list[tuple[float, float]]]]:
    """Extract hour-h-to-hour-1 ratios from comparable game launches.

    For each comparable with valid hourly data, compute the ratio of
    each hour's viewers to hour-1's viewers at each quantile level.
    Returns {hour: [[(ratio, weight), ...], ...]} across all comparables.
    """
    hour_ratios: dict[int, list[list[tuple[float, float]]]] = {h: [[] for _ in LEVELS] for h in range(2, 25)}

    for comp in comparables:
        name = comp.get("name", "")
        score = COMPARABLE_SCORES.get(name, 0.3)
        hourly = comp.get("hourly", [])

        if not hourly:
            continue

        # Find hour 1 closest to launch (first hour after unlock)
        unlock = comp.get("unlock", "")
        hour1 = None
        for entry in hourly:
            if entry.get("viewers", 0) > 0:
                hour1 = entry
                break

        if not hour1 or hour1["viewers"] < 1000:
            continue

        p1 = max(1.0, hour1["viewers"])

        # Map hours after unlock to hour indices
        for entry in hourly[1:]:
            viewers = max(1, entry.get("viewers", 0))
            ratio = viewers / p1

            # Determine which hour-of-day this maps to
            # Use position in list as proxy for hour offset
            h_idx = hourly.index(entry) + 1
            if h_idx < 2 or h_idx > 24:
                continue

            # Weight by similarity score
            weight = max(0.1, score)

            # For quantile estimation: each comparable contributes one
            # observation at each level, weighted by similarity
            for j in range(len(LEVELS)):
                hour_ratios[h_idx][j].append((ratio, weight))

    return hour_ratios


def weighted_quantile(values: list[tuple[float, float]], q: float) -> float:
    """Compute weighted quantile from (value, weight) pairs."""
    if not values:
        return 1.0

    sorted_vals = sorted(values, key=lambda x: x[0])
    total_weight = sum(w for _, w in sorted_vals)
    if total_weight == 0:
        return sorted_vals[len(sorted_vals) // 2][0]

    target = q * total_weight
    cumsum = 0.0
    for val, w in sorted_vals:
        cumsum += w
        if cumsum >= target:
            return val
    return sorted_vals[-1][0]


def compute_spread_from_comparables(comparables: list[dict]) -> float:
    """Learn log-spread sigma from comparable launch variance."""
    ratios = []
    for comp in comparables:
        name = comp.get("name", "")
        score = COMPARABLE_SCORES.get(name, 0.3)
        hourly = comp.get("hourly", [])
        if len(hourly) < 6:
            continue

        hour1_viewers = max(1, hourly[0].get("viewers", 0))

        for i, entry in enumerate(hourly[1:13], start=2):
            viewers = max(1, entry.get("viewers", 0))
            log_ratio = math.log(viewers / hour1_viewers)
            ratios.append((log_ratio, score * (1.0 / math.sqrt(i - 1))))

    if not ratios:
        return 0.15  # fallback to baseline

    # Weighted standard deviation of normalized log returns
    total_w = sum(w for _, w in ratios)
    mean = sum(v * w for v, w in ratios) / total_w
    var = sum(w * (v - mean) ** 2 for v, w in ratios) / total_w
    learned = math.sqrt(max(0.001, var))

    # Blend with prior (0.15) to avoid overfitting
    return 0.4 * 0.15 + 0.6 * learned


def compute_gta_history_trend(history_hourly: list[dict]) -> dict[int, float]:
    """Extract GTA VI's own pre-launch hourly pattern.

    Returns {hour_of_day: avg_viewers_ratio} based on daily patterns.
    """
    if not history_hourly:
        return {}

    # Group by hour of day, compute mean viewers
    hour_sums: dict[int, list[float]] = {}
    for entry in history_hourly:
        from_ts = entry.get("from", "")
        viewers = entry.get("viewers", 0)
        if not from_ts or viewers <= 0:
            continue
        try:
            # Parse hour from ISO timestamp
            h = int(from_ts[11:13])
            hour_sums.setdefault(h, []).append(viewers)
        except (ValueError, IndexError):
            continue

    if not hour_sums:
        return {}

    # Compute mean per hour, then normalize to hour-1 baseline
    hour_means = {h: sum(v) / len(v) for h, v in hour_sums.items()}
    baseline = hour_means.get(min(hour_means.keys()), 1)
    return {h: v / baseline for h, v in hour_means.items()}


def forecast(case: dict) -> list[dict]:
    """Generate 23-hour forecast for a single case."""
    known = case.get("known", [])
    forecast_hours = case.get("forecast", [])
    comparables = case.get("comparables", [])
    history_hourly = case.get("history_hourly", [])

    # Hour 1 print
    hour1 = next((k for k in known if k.get("hour") == 1), None)
    if not hour1:
        # Fallback: use first known entry
        hour1 = known[0] if known else {"viewers": 100000}
    p1 = max(1.0, hour1.get("viewers", 100000))

    # 1. Learn spread from comparables
    sigma = compute_spread_from_comparables(comparables)

    # 2. Get comparable-based ratios
    comp_ratios = compute_comparable_ratios(comparables)

    # 3. Get GTA VI history pattern
    gta_pattern = compute_gta_history_trend(history_hourly)

    results = []
    for fh in forecast_hours:
        h = fh.get("hour", 0)
        if h < 2 or h > 24:
            continue

        # Determine hour-of-day for history lookup
        # day_start gives the UTC hour of the release day start
        day_start = case.get("day_start", "")
        try:
            ds_hour = int(day_start[11:13])
        except (ValueError, IndexError):
            ds_hour = 8  # default: morning launch

        hod = ((ds_hour + h - 1) % 24)

        # Base quantile estimates
        q_vals = []

        for j in range(len(LEVELS)):
            # Start with persistence baseline
            base_q = p1 * math.exp(Z[j] * sigma * math.sqrt(h - 1))

            # Blend with comparable ratios if available
            if h in comp_ratios and comp_ratios[h][j]:
                comp_q = weighted_quantile(comp_ratios[h][j], LEVELS[j])
                # Blend: 60% comparable data, 40% persistence
                base_q = 0.6 * (p1 * comp_q) + 0.4 * base_q

            # Blend with GTA history pattern
            if hod in gta_pattern:
                hist_factor = gta_pattern[hod]
                base_q *= (0.3 * hist_factor + 0.7)

            # Clamp to reasonable range
            q_vals.append(max(1.0, min(1e9, base_q)))

        # Ensure non-decreasing
        for j in range(1, len(q_vals)):
            if q_vals[j] < q_vals[j - 1]:
                q_vals[j] = q_vals[j - 1]

        results.append({"hour": h, "q": [round2(v) for v in q_vals]})

    return results


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Read every .in file, sorted for deterministic output
    inputs = sorted(
        f for f in os.listdir(INPUT_DIR) if f.endswith(".in")
    )

    for file in inputs:
        case_id = file[:-3]  # remove .in
        with open(os.path.join(INPUT_DIR, file), "r") as f:
            case = json.load(f)

        # Verify levels match
        case_levels = case.get("levels", LEVELS)
        if len(case_levels) != len(LEVELS) or any(
            abs(a - b) > 1e-9 for a, b in zip(case_levels, LEVELS)
        ):
            raise ValueError(
                f"{file}: levels {case_levels} != {LEVELS}"
            )

        hours = forecast(case)
        output = {"hours": hours}

        with open(os.path.join(OUTPUT_DIR, f"{case_id}.out"), "w") as f:
            json.dump(output, f)
            f.write("\n")


if __name__ == "__main__":
    main()