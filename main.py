"""Hour-by-hour Twitch CCU forecast for a game launch day, from its first hour.

Model: comparable-launch regression. Every case ships the release-day curves of
dozens of past big launches ("comparables"). For each hour h = 2..24 we regress
the log ratio ln(viewers_h / viewers_1) of the comparable launches on the log
of their own hour-1 print, then predict the case's ratio at the case's hour-1
print. The spread comes from the empirical quantiles of that hour's regression
residuals. Three facts drive this shape:

1. Launch days share a diurnal + unlock-roll shape (night dip, US unlock steps
   at 05:00/08:00 UTC, evening peak), so ratios to hour 1 transfer across
   launches far better than absolute counts.
2. The ratio shrinks with launch scale: a huge hour-1 print means the launch
   was already near its peak in hour 1, so the evening multiple is smaller.
   The log-log regression captures that instead of assuming one shape for all.
3. Launches whose hour 1 predates their unlock (a single global unlock later
   in the day, slow-burn starts) have hour-1 prints that are pre-launch
   chatter, not a launch signal. They are filtered out by curve shape: hour 1
   must be at least 5% of the launch's release-day peak. Early-access and
   weak-tier launches are excluded too, from the tier marker in their unlock
   notes; launches without a tier marker are kept when their shape qualifies.

Guards: the prediction's log hour-1 is clamped to the pool's observed range so
an unprecedented print cannot extrapolate the line; hours with too few pool
points fall back to empirical quantiles, then to persistence with a widening
spread. Everything is computed from the case file alone: no network, no
hardcoded answer, no state between runs. Standard library only.
"""

import calendar
import json
import math
import os
import re
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# Standard normal quantiles at LEVELS, for the fallback spread only.
Z_SCORES = [-1.6448536269514722, -0.6744897501960817, 0.0,
            0.6744897501960817, 1.6448536269514722]
# Random-walk spread (log units per sqrt hour) used when the pool is too thin.
FALLBACK_SIGMA = 0.15
# A comparable qualifies as "launched by hour 1" when hour 1 reaches this
# fraction of its release-day peak.
LAUNCHED_FRACTION = 0.05
# Minimum pool points for the per-hour regression; fewer falls back.
MIN_REGRESSION_POINTS = 5
MIN_QUANTILE_POINTS = 3


def epoch_seconds(stamp):
    """Epoch seconds for an ISO-8601 UTC stamp (whole seconds, 'Z' suffix)."""
    return calendar.timegm((
        int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10]),
        int(stamp[11:13]), int(stamp[14:16]), int(stamp[17:19]),
        0, 0, 0))


def release_day_curve(comparable):
    """Map hour 1..24 -> viewers for the comparable's release day.

    Returns None when the comparable has no positive hour-1 point: without
    hour 1 there is nothing to normalise by, and the GTA VI pre-release
    history comparable (whose day holds no launch) drops out here.
    """
    try:
        day_start = epoch_seconds(comparable["day_start"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    curve = {}
    hourly = comparable.get("hourly") or []
    if not isinstance(hourly, list):
        return None
    for point in hourly:
        if not isinstance(point, dict):
            continue
        stamp = point.get("from") or point.get("at")
        viewers = point.get("viewers")
        if not isinstance(stamp, str) or len(stamp) < 19:
            continue
        if not isinstance(viewers, (int, float)) or isinstance(viewers, bool):
            continue
        try:
            hour = int((epoch_seconds(stamp) - day_start) // 3600) + 1
        except (TypeError, ValueError, IndexError):
            continue
        if 1 <= hour <= 24 and viewers > 0:
            curve[hour] = viewers
    if curve.get(1, 0) <= 0:
        return None
    return curve


def tier_marker(comparable):
    """The tier word from the comparable's unlock note, e.g. 'comparable'."""
    match = re.search(r"Tier:\s*([A-Za-z\-]+)", comparable.get("unlock") or "")
    return match.group(1).strip().lower() if match else ""


def launched_by_hour_one(curve):
    """True when hour 1 is already a launch hour, not pre-launch chatter."""
    peak = max((curve[h] for h in range(2, 25) if h in curve), default=0)
    return peak > 0 and curve[1] / peak >= LAUNCHED_FRACTION


def build_pool(case):
    """Comparable curves usable for the regression, most similar first cut.

    Falls back to looser pools when the strict one is too small, so the entry
    always has something to regress on; returns [] only when the case ships no
    usable comparable at all.
    """
    named = []
    for comparable in case.get("comparables") or []:
        curve = release_day_curve(comparable)
        if curve is not None:
            named.append((comparable.get("name") or "", tier_marker(comparable), curve))
    # Deterministic order regardless of the file's listing order.
    named.sort(key=lambda item: item[0])

    def excluded_tier(tier):
        return "early" in tier or "weak" in tier or "target" in tier

    strict = [curve for _, tier, curve in named
              if not excluded_tier(tier) and launched_by_hour_one(curve)]
    if len(strict) >= MIN_REGRESSION_POINTS:
        return strict
    shaped = [curve for _, _, curve in named if launched_by_hour_one(curve)]
    if len(shaped) >= MIN_REGRESSION_POINTS:
        return shaped
    all_curves = [curve for _, _, curve in named]
    if len(all_curves) >= MIN_REGRESSION_POINTS:
        return all_curves
    return strict or shaped or all_curves


def interpolated_quantile(ordered, level):
    """Linear-interpolation quantile of an already sorted sample."""
    count = len(ordered)
    if count == 0:
        return 0.0
    if count == 1:
        return ordered[0]
    position = level * (count - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, count - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def least_squares(xs, ys):
    """Intercept and slope of the ordinary least-squares line."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread <= 0:
        return mean_y, 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / spread
    return mean_y - slope * mean_x, slope


def round_half_up(value):
    """Round to 2 decimals, halves away from zero; whole numbers as ints."""
    scaled = value * 100.0
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    rounded = whole / 100.0
    return int(rounded) if rounded.is_integer() else rounded


def clean_quantiles(raw):
    """Clamp to the valid range and enforce non-decreasing order."""
    cleaned = []
    previous = 0.01
    for value in raw:
        clamped = min(max(value, 0.01), 1e9)
        clamped = max(clamped, previous)
        cleaned.append(round_half_up(clamped))
        previous = cleaned[-1]
    return cleaned


def hour_forecast(hour, p1, pool):
    """Five quantiles for one hour, from the pool's log ratios at that hour."""
    points = []
    for curve in pool:
        if hour in curve:
            points.append((math.log(curve[1]), math.log(curve[hour] / curve[1])))
    log_p1 = math.log(p1)
    if len(points) >= MIN_REGRESSION_POINTS:
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        intercept, slope = least_squares(xs, ys)
        # Never extrapolate past the launches actually observed.
        query = min(max(log_p1, min(xs)), max(xs))
        centre = intercept + slope * query
        residuals = sorted(y - (intercept + slope * x) for x, y in points)
        log_quantiles = [centre + interpolated_quantile(residuals, level)
                         for level in LEVELS]
    elif len(points) >= MIN_QUANTILE_POINTS:
        ratios = sorted(point[1] for point in points)
        log_quantiles = [interpolated_quantile(ratios, level) for level in LEVELS]
    else:
        centre = 0.0
        if points:
            centre = interpolated_quantile(sorted(p[1] for p in points), 0.5)
        width = FALLBACK_SIGMA * math.sqrt(hour - 1)
        log_quantiles = [centre + z * width for z in Z_SCORES]
    return clean_quantiles([p1 * math.exp(q) for q in log_quantiles])


def hour_one_print(case):
    """Hour 1's print, with fallbacks if the known row is missing or empty."""
    for row in case.get("known") or []:
        if row.get("hour") == 1 and row.get("viewers"):
            return max(1.0, float(row["viewers"]))
    hourly = case.get("history_hourly") or []
    if hourly:
        last = hourly[-1]
        if last.get("viewers"):
            return max(1.0, float(last["viewers"]))
    return 1.0


def persistence_forecast(p1):
    """The fallback: every hour stays at hour 1, spread widening like a walk."""
    forecast = {}
    for hour in range(2, 25):
        width = FALLBACK_SIGMA * math.sqrt(hour - 1)
        forecast[hour] = clean_quantiles(
            [p1 * math.exp(z * width) for z in Z_SCORES])
    return forecast


def forecast_case(case):
    p1 = hour_one_print(case)
    try:
        pool = build_pool(case)
    except Exception:
        pool = []
    if not pool:
        return persistence_forecast(p1)
    try:
        return {hour: hour_forecast(hour, p1, pool) for hour in range(2, 25)}
    except Exception:
        return persistence_forecast(p1)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
            hours = forecast_case(case)
        except Exception:
            hours = persistence_forecast(1.0)
        payload = {"hours": [{"hour": hour, "q": hours[hour]}
                             for hour in range(2, 25)]}
        text = json.dumps(payload, separators=(",", ":")) + "\n"
        (OUTPUT_DIR / (path.stem + ".out")).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
