"""Weather via free Open-Meteo (no API key)."""
import requests

DEFAULT_LOC = {"name": "New Smyrna Beach, FL", "lat": 29.0258, "lon": -80.927}
WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
       81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}


def geocode(place):
    q = place.split(",")[0].strip()
    r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                     params={"name": q, "count": 5, "language": "en", "format": "json"}, timeout=15)
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        return None
    want = place.lower()
    best = next((x for x in res if (x.get("admin1") or "").lower() in want or "fl" in want and x.get("admin1") == "Florida"), res[0])
    name = ", ".join(p for p in [best.get("name"), best.get("admin1"), best.get("country_code")] if p)
    return {"name": name, "lat": best["latitude"], "lon": best["longitude"]}


def get_weather(location=None):
    try:
        loc = DEFAULT_LOC
        if location and location.strip().lower() not in ("", "here", "new smyrna beach", "new smyrna beach, fl", "new smyrna"):
            loc = geocode(location) or {"error": f"could not geocode {location}"}
            if "error" in loc:
                return loc
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": loc["lat"], "longitude": loc["lon"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,wind_gusts_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "timezone": "America/New_York", "forecast_days": 1}, timeout=15)
        r.raise_for_status()
        j = r.json(); c = j["current"]; d = j["daily"]
        return {"source": "open-meteo", "location": loc["name"], "time": c.get("time"),
                "temp_f": c.get("temperature_2m"), "feels_like_f": c.get("apparent_temperature"),
                "conditions": WMO.get(c.get("weather_code"), f"code {c.get('weather_code')}"),
                "wind_mph": c.get("wind_speed_10m"), "gust_mph": c.get("wind_gusts_10m"),
                "humidity_pct": c.get("relative_humidity_2m"),
                "high_f": d["temperature_2m_max"][0], "low_f": d["temperature_2m_min"][0],
                "rain_chance_pct": d["precipitation_probability_max"][0]}
    except Exception as e:
        return {"error": str(e), "source": "open-meteo"}


def format_weather(w):
    if not w or w.get("error"):
        return f"Weather unavailable: {(w or {}).get('error')}"
    return (f"**Weather — {w['location']}** (Open-Meteo, {w['time']} ET)\n"
            f"* Now: {w['temp_f']:.0f}°F (feels {w['feels_like_f']:.0f}°F), {w['conditions']}\n"
            f"* Wind: {w['wind_mph']:.0f} mph (gusts {w['gust_mph']:.0f}), humidity {w['humidity_pct']}%\n"
            f"* Today: high {w['high_f']:.0f}°F / low {w['low_f']:.0f}°F, rain chance {w['rain_chance_pct']}%")


def get_weather_period(location=None, period="afternoon"):
    """period: afternoon (12-6pm today), tonight (6pm-6am), tomorrow (daily + hourly)."""
    import datetime as dt
    base = get_weather(location)
    if base.get("error"):
        return base
    loc = DEFAULT_LOC
    if location and location.strip().lower() not in ("", "here", "new smyrna beach", "new smyrna beach, fl", "new smyrna"):
        loc = geocode(location) or DEFAULT_LOC
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": loc["lat"], "longitude": loc["lon"],
            "hourly": "temperature_2m,precipitation_probability,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "America/New_York", "forecast_days": 3}, timeout=15)
        r.raise_for_status(); j = r.json()
    except Exception as e:
        return {"error": str(e)}
    h = j["hourly"]; today = dt.date.fromisoformat(j["daily"]["time"][0])
    if period == "tomorrow":
        d = j["daily"]; i = 1
        return {**base, "period": "tomorrow", "high_f": d["temperature_2m_max"][i], "low_f": d["temperature_2m_min"][i],
                "rain_chance_pct": d["precipitation_probability_max"][i], "conditions": WMO.get(d["weather_code"][i], "?")}
    if period == "tonight":
        lo = dt.datetime.combine(today, dt.time(18)); hi = lo + dt.timedelta(hours=12)
    else:
        lo = dt.datetime.combine(today, dt.time(12)); hi = dt.datetime.combine(today, dt.time(18))
    idx = [k for k, t in enumerate(h["time"]) if lo <= dt.datetime.fromisoformat(t) < hi]
    temps = [h["temperature_2m"][k] for k in idx]
    codes = [h["weather_code"][k] for k in idx]
    return {**base, "period": period, "p_temp_min": min(temps), "p_temp_max": max(temps),
            "p_rain_max": max(h["precipitation_probability"][k] or 0 for k in idx),
            "p_wind_max": max(h["wind_speed_10m"][k] for k in idx),
            "p_conditions": WMO.get(max(set(codes), key=codes.count), "?"), "p_window": f"{lo:%-I%p}-{hi:%-I%p}"}


def format_weather_period(w):
    if not w or w.get("error"):
        return f"Weather unavailable: {(w or {}).get('error')}"
    if w["period"] == "tomorrow":
        return (f"**Weather tomorrow — {w['location']}** (Open-Meteo)\n* {w['conditions']}, high {w['high_f']:.0f}°F / "
                f"low {w['low_f']:.0f}°F, rain chance {w['rain_chance_pct']}%")
    return (f"**Weather {w['period']} ({w['p_window']}) — {w['location']}** (Open-Meteo hourly)\n"
            f"* {w['p_conditions']}, {w['p_temp_min']:.0f}–{w['p_temp_max']:.0f}°F, rain chance up to {w['p_rain_max']}%, "
            f"wind up to {w['p_wind_max']:.0f} mph\n* Right now: {w['temp_f']:.0f}°F (feels {w['feels_like_f']:.0f}°F), {w['conditions']}")
