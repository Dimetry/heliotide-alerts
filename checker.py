#!/usr/bin/env python3
"""Heliotide alerts checker.

Runs every 5 minutes on GitHub Actions. Reads official sources (NOAA SWPC, USGS, GDACS,
NOAA tsunami centers), finds important new events and writes one "Alert" record per event
to the CloudKit public database of the Heliotide app. Phones subscribe to these records
(CKQuerySubscription) and Apple delivers the push. No user data is stored here.

Standard library only. The CloudKit request is signed with `openssl` (installed on GitHub runners).

Record fields:
  kind   one of: storm, flare, radiation, wind (worldwide), nearby, aurora (by area)
  title  short line for the notification
  body   one plain sentence
  cells  ["global"] for worldwide events, or the 5° x 5° grid cells the event can affect
  issued time of the event

Usage:
  python3 checker.py            check sources; without CloudKit secrets it only prints (dry run)
  python3 checker.py --test storm   send one test alert of that kind to everyone subscribed
  python3 checker.py --test nearby --cell 9_-25   test alert for one area cell
"""

import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

CONTAINER = "iCloud.dev.spacecast.app"
ENVIRONMENT = os.environ.get("CK_ENV", "development")
KEY_ID = os.environ.get("CK_KEY_ID", "")
PRIVATE_KEY = os.environ.get("CK_PRIVATE_KEY", "")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
CELL_DEG = 1  # about 110 km; must match AlertsService.cellDegrees in the app
NOW = dt.datetime.now(dt.timezone.utc)

SWPC = "https://services.swpc.noaa.gov"


# ---------- helpers ----------

def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "heliotide-alerts/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url):
    return json.loads(get(url))


def parse_time(text):
    """NOAA / GDACS times: '2026-11-19 01:18:16.670' or ISO, always UTC."""
    t = text.strip().replace(" ", "T").rstrip("Z")
    t = t.split(".")[0]
    return dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)


def hours_ago(t):
    return (NOW - t).total_seconds() / 3600


def cell(lat, lon):
    lon = ((lon + 180) % 360) - 180
    return f"{math.floor(lat / CELL_DEG)}_{math.floor(lon / CELL_DEG)}"


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(a)))


def cells_within(lat, lon, km):
    """Every grid cell that has at least one point within `km` of the event."""
    out = []
    for i in range(-90 // CELL_DEG, 90 // CELL_DEG):
        la0, la1 = i * CELL_DEG, (i + 1) * CELL_DEG
        near_lat = min(max(lat, la0), la1)
        for j in range(-180 // CELL_DEG, 180 // CELL_DEG):
            lo0 = j * CELL_DEG
            d = ((lon - lo0) % 360)          # event east of the cell's west edge, 0..360
            near_lon = lon if d <= CELL_DEG else (lo0 + CELL_DEG if d - CELL_DEG < 360 - d else lo0)
            if distance_km(lat, lon, near_lat, near_lon) <= km:
                out.append(f"{i}_{j}")
    return out


class Event:
    """One alert. Worldwide kinds have no zones. Local kinds have three zones of grid cells:
    zone1 = close / strong, zone2 = middle, zone3 = far / weak (see ZONES). The phone subscribes
    to the zone the person chose in Settings, and to `level >= ` their minimum strength."""

    def __init__(self, key, kind, title, body, issued, level=0.0, zones=None, lat=None, lon=None):
        self.key = re.sub(r"[^A-Za-z0-9_.-]", "-", key)[:200]
        self.kind, self.title, self.body, self.issued = kind, title, body, issued
        self.level, self.zones, self.lat, self.lon = float(level), zones, lat, lon

    def __repr__(self):
        where = "global" if not self.zones else "/".join(str(len(z)) for z in self.zones) + " cells"
        return f"[{self.kind} {self.level:g}] {self.title} — {self.body} ({where}) key={self.key}"


# Distances (km) of the three zones per local kind. The app offers the same three choices.
ZONES = {"quake": (300, 1000, 2000), "tsunami": (500, 1000, 2000), "cyclone": (300, 600, 1000),
         "flood": (100, 300, 600), "volcano": (100, 300, 600), "fire": (50, 100, 300)}


def zones_for(kind, lat, lon):
    return [cells_within(lat, lon, km) for km in ZONES[kind]]


WORD = {1: "minor", 2: "moderate", 3: "strong", 4: "severe", 5: "extreme"}


def near_place(place):
    """USGS '35 km SW of Kodiak, Alaska' → 'near Kodiak, Alaska'; 'southeast of the Loyalty Islands' stays."""
    place = (place or "").strip()
    short = re.sub(r"^\d+\s*km\s+[NSEW]{1,3}\s+of\s+", "", place)
    if not short:
        return "far from towns"
    return f"near {short}" if short != place or not re.match(r"(?i)(north|south|east|west|off|central)", short) else short


# ---------- sources ----------

def noaa_alerts():
    """NOAA SWPC alerts: magnetic storm reached, strong flares, radiation storms."""
    events = []
    for a in get_json(f"{SWPC}/products/alerts.json"):
        msg = a.get("message", "")
        code = re.search(r"Message Code:\s*(\w+)", msg)
        serial = re.search(r"Serial Number:\s*(\d+)", msg)
        if not code or not serial:
            continue
        code, serial = code.group(1), serial.group(1)
        issued = parse_time(a["issue_datetime"])
        if hours_ago(issued) > 6:
            continue
        m = re.fullmatch(r"ALTK0([5-9])", code)
        if m:
            g = int(m.group(1)) - 4
            # NOAA repeats "Kp 5 reached" during one storm: one push per level per UTC day.
            events.append(Event(f"storm-G{g}-{issued:%Y%m%d}", "storm", f"{WORD[g].capitalize()} magnetic storm",
                                "Aurora may be seen farther from the poles than usual. GPS and radio may be less accurate.",
                                issued, level=g))
        elif code == "ALTXMF":
            events.append(Event(f"noaa-{code}-{serial}", "flare", "Strong solar flare",
                                "Short radio blackouts are possible on the sunlit side of Earth.", issued, level=1))
        elif code == "SUMX01":
            cls = re.search(r"X-ray Class:\s*([A-Z][\d.]+)", msg)
            name = cls.group(1) if cls else "X"
            events.append(Event(f"noaa-{code}-{serial}", "flare", "Very strong solar flare",
                                f"The strongest class ({name}). Radio may fade on the sunlit side of Earth.",
                                issued, level=2))
        else:
            m = re.fullmatch(r"ALTPX([1-3])", code)
            if m:
                s_ = int(m.group(1))
                events.append(Event(f"noaa-{code}-{serial}", "radiation", "Radiation storm",
                                    f"{WORD[s_].capitalize()}. Satellites and polar flights may be affected. No risk on the ground.",
                                    issued, level=s_))
    return events


def fast_wind():
    rows = get_json(f"{SWPC}/products/summary/solar-wind-speed.json")
    if not rows:
        return []
    r = rows[0]
    speed = float(r.get("proton_speed") or 0)
    if speed < 600:
        return []
    issued = parse_time(r["time_tag"])
    return [Event(f"wind-{issued:%Y%m%d}", "wind", "Fast solar wind",
                  f"{round(speed)} km/s, faster than usual. Storms and aurora become more likely.", issued, level=1)]


def storm_now():
    """Kp 5 or more right now (estimated, 1-minute data)."""
    rows = get_json(f"{SWPC}/json/planetary_k_index_1m.json")
    return bool(rows) and float(rows[-1].get("estimated_kp") or 0) >= 5


def aurora():
    """During a storm: one record per hemisphere. Zones by chance: zone1 50%+, zone2 30%+, zone3 10%+."""
    if not storm_now():
        return []
    grid = get_json(f"{SWPC}/json/ovation_aurora_latest.json")["coordinates"]
    half = "am" if NOW.hour < 12 else "pm"
    events = []
    for name, north in (("north", True), ("south", False)):
        best = {}
        for lon, lat, p in grid:
            if (lat >= 0) == north and p >= 10:
                c = cell(lat, lon)
                best[c] = max(best.get(c, 0), p)
        if not best:
            continue
        zones = [sorted(c for c, p in best.items() if p >= t) for t in (50, 30, 10)]
        events.append(Event(f"aurora-{name}-{NOW:%Y%m%d}-{half}", "aurora", "Aurora possible tonight",
                            f"Look {name} after dark, away from city lights. Best if the sky is clear.",
                            NOW, zones=zones))
    return events


def quakes():
    """Earthquakes M5.5+. The person chooses the strength and distance in Settings."""
    events = []
    feed = get_json("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson")
    for f in feed["features"]:
        p = f["properties"]
        mag = p.get("mag") or 0
        if mag < 5.5:
            continue
        issued = dt.datetime.fromtimestamp(p["time"] / 1000, dt.timezone.utc)
        if hours_ago(issued) > 6:
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        word = "Major earthquake" if mag >= 7 else "Strong earthquake" if mag >= 6 else "Earthquake"
        events.append(Event(f"quake-{f['id']}", "quake", f"{word} near you",
                            f"Magnitude {mag:.1f}, {near_place(p.get('place'))}.",
                            issued, level=mag, zones=zones_for("quake", lat, lon), lat=lat, lon=lon))
    return events


GDACS_KIND = {"TC": "cyclone", "FL": "flood", "VO": "volcano", "WF": "fire"}


def gdacs():
    """UN/EU disaster alerts, orange (level 2) and red (3). A new episode at a higher level alerts again."""
    url = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
           "?eventlist=TC;FL;VO;WF&alertlevel=orange;red")
    events = []
    for f in get_json(url).get("features", []):
        p = f["properties"]
        kind = GDACS_KIND.get(p.get("eventtype"))
        if str(p.get("iscurrent")).lower() != "true" or not kind:
            continue
        level_name = (p.get("alertlevel") or "").lower()
        level = 3 if level_name == "red" else 2
        lon, lat = f["geometry"]["coordinates"][:2]
        country = p.get("country") or "your region"
        name = re.sub(r"^(Tropical Cyclone|Volcano)\s+", "", p.get("name") or "").split("-")[0].strip().title()
        if kind == "cyclone":
            title, body = "Tropical cyclone near you", f"{name}, {level_name} alert, {country}. Follow local officials."
        elif kind == "flood":
            title, body = "Flood alert near you", f"{level_name.capitalize()} alert in {country}. Follow local officials."
        elif kind == "volcano":
            title, body = "Volcano alert near you", f"{name}, {country}, {level_name} alert."
        else:
            title, body = "Large wildfire near you", f"{level_name.capitalize()} alert in {country}. Smoke may reach your area."
        events.append(Event(f"gdacs-{p['eventtype']}-{p['eventid']}-{level_name}", kind, title, body, NOW,
                            level=level, zones=zones_for(kind, lat, lon), lat=lat, lon=lon))
    return events


def tsunami():
    """NOAA tsunami centers (Alaska: US/Canada coasts; Hawaii: Pacific). Not 'Information' bulletins."""
    ns = {"a": "http://www.w3.org/2005/Atom", "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#"}
    rank = {"Watch": 1, "Advisory": 2, "Threat": 2, "Warning": 3}
    events = []
    for feed in ("PAAQ", "PHEB"):
        root = ET.fromstring(get(f"https://www.tsunami.gov/events/xml/{feed}Atom.xml"))
        for e in root.findall("a:entry", ns):
            summary = ET.tostring(e.find("a:summary", ns), encoding="unicode", method="text")
            cat = re.search(r"Category:\s*(\w+)", summary)
            if not cat or cat.group(1) not in rank:
                continue
            issued = parse_time(e.findtext("a:updated", "", ns))
            if hours_ago(issued) > 6:
                continue
            lat, lon = float(e.findtext("geo:lat", "0", ns)), float(e.findtext("geo:long", "0", ns))
            region = near_place(e.findtext("a:title", "", ns).strip())
            events.append(Event(f"tsunami-{e.findtext('a:id', '', ns)}", "tsunami",
                                f"Tsunami {cat.group(1).lower()} near you",
                                f"{region[0].upper() + region[1:]}. Follow local officials.",
                                issued, level=rank[cat.group(1)], zones=zones_for("tsunami", lat, lon), lat=lat, lon=lon))
    return events


# ---------- CloudKit ----------

def fields(event):
    f = {"kind": {"value": event.kind},
         "title": {"value": event.title},
         "body": {"value": event.body},
         "level": {"value": event.level, "type": "DOUBLE"},
         "issued": {"value": int(event.issued.timestamp() * 1000), "type": "TIMESTAMP"}}
    for i, cells in enumerate(event.zones or [], start=1):
        if cells:   # CloudKit rejects empty lists
            f[f"zone{i}"] = {"value": cells, "type": "STRING_LIST"}
    if event.lat is not None:
        f["lat"] = {"value": float(event.lat), "type": "DOUBLE"}
        f["lon"] = {"value": float(event.lon), "type": "DOUBLE"}
    return f


def cloudkit_create(event):
    """Creates the Alert record. Returns True when written (or it was already there)."""
    path = f"/database/1/{CONTAINER}/{ENVIRONMENT}/public/records/modify"
    body = json.dumps({"operations": [{"operationType": "create", "record": {
        "recordType": "Alert", "recordName": event.key,
        "fields": fields(event)}}]}, separators=(",", ":")).encode()
    date = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    message = f"{date}:{digest}:{path}".encode()
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as k:
        k.write(PRIVATE_KEY)
        key_path = k.name
    try:
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_path], input=message,
                             capture_output=True, check=True).stdout
    finally:
        os.unlink(key_path)
    req = urllib.request.Request("https://api.apple-cloudkit.com" + path, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Apple-CloudKit-Request-KeyID": KEY_ID,
        "X-Apple-CloudKit-Request-ISO8601Date": date,
        "X-Apple-CloudKit-Request-SignatureV1": base64.b64encode(sig).decode(),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  CloudKit HTTP {e.code}: {e.read().decode()[:300]}")
        return False
    rec = (result.get("records") or [{}])[0]
    if "serverErrorCode" in rec:
        if rec["serverErrorCode"] in ("RECORD_EXISTS", "CONFLICT"):
            return True
        print(f"  CloudKit error: {rec.get('serverErrorCode')} {rec.get('reason', '')}")
        return False
    return True


# ---------- main ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"seeded": False, "sent": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


def main():
    args = sys.argv[1:]
    live = bool(KEY_ID and PRIVATE_KEY)

    if "--test" in args:
        kind = args[args.index("--test") + 1]
        cell_arg = args[args.index("--cell") + 1] if "--cell" in args else None
        zones = [[cell_arg]] * 3 if cell_arg else None
        e = Event(f"test-{NOW:%Y%m%d%H%M%S}-{kind}", kind, "Heliotide test alert",
                  "If you see this, alerts work.", NOW, level=99, zones=zones)
        print("TEST", e)
        ok = cloudkit_create(e) if live else True
        print("sent" if ok and live else "dry run (no CloudKit key)" if ok else "FAILED")
        sys.exit(0 if ok else 1)

    events, failed = [], []
    for source in (noaa_alerts, fast_wind, aurora, quakes, gdacs, tsunami):
        try:
            events += source()
        except Exception as ex:  # one broken source must not stop the others
            failed.append(f"{source.__name__}: {ex}")

    state = load_state()
    sent = state.setdefault("sent", {})
    new = [e for e in events if e.key not in sent]
    print(f"{len(events)} current events, {len(new)} new" + (f"; failed sources: {failed}" if failed else ""))
    if "--all" in args:
        for e in events:
            print("  current:", e)

    if not state.get("seeded"):
        # First run: remember what is already going on, alert only on what comes after.
        for e in new:
            sent[e.key] = NOW.isoformat(timespec="seconds")
        state["seeded"] = True
        print(f"first run: {len(new)} existing events remembered, nothing sent")
    else:
        for e in new:
            print(" ", e)
            if not live:
                continue
            if cloudkit_create(e):
                sent[e.key] = NOW.isoformat(timespec="seconds")

    # Forget keys after 30 days (events end long before that).
    limit = NOW - dt.timedelta(days=30)
    state["sent"] = {k: v for k, v in sent.items() if dt.datetime.fromisoformat(v) > limit}
    if live or "--save" in args:
        save_state(state)
    if failed:
        print("WARNING: some sources failed", file=sys.stderr)


if __name__ == "__main__":
    main()
