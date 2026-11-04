"""NOAA CO-OPS tide predictions (free, no key). Default: New Smyrna Beach station 8721164."""
import datetime
import requests

STATION = {"id": "8721164", "name": "New Smyrna Beach, FL (NOAA 8721164)"}
API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


def get_tides(station=None, days=3):
    st = station or STATION
    now = datetime.datetime.now()
    try:
        r = requests.get(API, params={"product": "predictions", "application": "pyth", "station": st["id"],
                                      "begin_date": now.strftime("%Y%m%d"), "range": 24 * days, "datum": "MLLW",
                                      "interval": "hilo", "units": "english", "time_zone": "lst_ldt", "format": "json"}, timeout=20)
        r.raise_for_status()
        preds = r.json().get("predictions") or []
    except Exception as e:
        return {"error": str(e), "source": "noaa"}
    out = [{"time": datetime.datetime.strptime(p["t"], "%Y-%m-%d %H:%M"), "type": "high" if p["type"] == "H" else "low",
            "ft": float(p["v"])} for p in preds]
    return {"source": "noaa", "station": st["name"], "now": now, "tides": out}


def format_tides(t, when=None):
    if t.get("error") or not t.get("tides"):
        return f"Tide data unavailable ({t.get('error', 'no predictions')})."
    now = t["now"]
    fmt = lambda x: f"{x['type'].title()} tide {x['time'].strftime('%a %-I:%M %p')} ET ({x['ft']:.1f} ft)"
    if when == "tomorrow":
        day = (now + datetime.timedelta(days=1)).date()
        rows = [x for x in t["tides"] if x["time"].date() == day]
        head = f"**Tides tomorrow — {t['station']}**"
    else:
        rows = [x for x in t["tides"] if x["time"] > now][:4]
        nh = next((x for x in rows if x["type"] == "high"), None)
        head = f"**Tides — {t['station']}**" + (f"\n* Next high tide: {nh['time'].strftime('%a %-I:%M %p')} ET ({nh['ft']:.1f} ft)" if nh else "")
    return head + "\n" + "\n".join("* " + fmt(x) for x in rows) + "\n(NOAA predictions, MLLW datum)"
