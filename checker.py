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
CELL_DEG = 5  # must match AlertsService.cell(for:) in the app
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
    def __init__(self, key, kind, title, body, cells, issued):
        self.key = re.sub(r"[^A-Za-z0-9_.-]", "-", key)[:200]
        self.kind, self.title, self.body, self.cells, self.issued = kind, title, body, cells, issued

    def __repr__(self):
        where = "global" if self.cells == ["global"] else f"{len(self.cells)} cells"
        return f"[{self.kind}] {self.title} — {self.body} ({where}) key={self.key}"


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
        key = f"noaa-{code}-{serial}"
        m = re.fullmatch(r"ALTK0([5-9])", code)
        if m:
            k = int(m.group(1))
            # NOAA repeats "Kp 5 reached" during one storm: one push per level per UTC day.
            events.append(Event(f"storm-G{k - 4}-{issued:%Y%m%d}", "storm", f"Magnetic storm G{k - 4}",
                                f"Kp {k} reached. Aurora may reach lower latitudes; GPS and radio can be disturbed.",
                                ["global"], issued))
        elif code == "ALTXMF":
            events.append(Event(key, "flare", "Strong solar flare",
                                "X-rays passed class M5. Short radio blackouts are possible on the day side of Earth.",
                                ["global"], issued))
        elif code == "SUMX01":
            cls = re.search(r"X-ray Class:\s*([A-Z][\d.]+)", msg)
            name = cls.group(1) if cls else "X"
            events.append(Event(key, "flare", f"{name} solar flare",
                                "An X-class flare: the strongest kind. Radio blackouts are possible on the day side of Earth.",
                                ["global"], issued))
        else:
            m = re.fullmatch(r"ALTPX([1-3])", code)
            if m:
                s = int(m.group(1))
                events.append(Event(key, "radiation", f"Radiation storm S{s}",
                                    "High-energy protons are raised. A risk for satellites and polar flights, not on the ground.",
                                    ["global"], issued))
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
                  f"{round(speed)} km/s. Magnetic storms and aurora become more likely.", ["global"], issued)]


def storm_now():
    """Kp 5 or more right now (estimated, 1-minute data)."""
    rows = get_json(f"{SWPC}/json/planetary_k_index_1m.json")
    return bool(rows) and float(rows[-1].get("estimated_kp") or 0) >= 5


def aurora():
    """During a storm: one record for every area where OVATION gives a real chance of aurora."""
    if not storm_now():
        return []
    grid = get_json(f"{SWPC}/json/ovation_aurora_latest.json")["coordinates"]
    cells = sorted({cell(lat, lon) for lon, lat, p in grid if p >= 20})
    if not cells:
        return []
    half = "am" if NOW.hour < 12 else "pm"
    return [Event(f"aurora-{NOW:%Y%m%d}-{half}", "aurora", "Aurora possible tonight",
                  "A magnetic storm is on. After dark, look toward the pole, away from city lights.",
                  cells, NOW)]


def quakes():
    """Strong earthquakes. The stronger, the farther they matter."""
    events = []
    feed = get_json("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson")
    for f in feed["features"]:
        p = f["properties"]
        mag = p.get("mag") or 0
        km = 2000 if mag >= 7 else 1000 if mag >= 6 else 300 if mag >= 5.5 else 0
        if not km:
            continue
        issued = dt.datetime.fromtimestamp(p["time"] / 1000, dt.timezone.utc)
        if hours_ago(issued) > 6:
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        events.append(Event(f"quake-{f['id']}", "nearby", f"M{mag:.1f} earthquake",
                            f"{p.get('place') or 'Strong earthquake'}. Source: USGS.",
                            cells_within(lat, lon, km), issued))
    return events


GDACS_TYPES = {"TC": ("Tropical cyclone", 1000), "FL": ("Flood", 300),
               "VO": ("Volcano", 300), "WF": ("Wildfire", 200)}


def gdacs():
    """UN/EU disaster alerts, orange and red only. A new episode at a higher level alerts again."""
    url = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
           "?eventlist=TC;FL;VO;WF&alertlevel=orange;red")
    events = []
    for f in get_json(url).get("features", []):
        p = f["properties"]
        if str(p.get("iscurrent")).lower() != "true" or p.get("eventtype") not in GDACS_TYPES:
            continue
        kind_name, km = GDACS_TYPES[p["eventtype"]]
        level = p.get("alertlevel", "")
        lon, lat = f["geometry"]["coordinates"][:2]
        name = p.get("name") or kind_name
        country = p.get("country") or ""
        events.append(Event(f"gdacs-{p['eventtype']}-{p['eventid']}-{level}", "nearby", name,
                            f"{level} alert from GDACS" + (f" · {country}" if country else "") + ".",
                            cells_within(lat, lon, km), NOW))
    return events


def tsunami():
    """NOAA tsunami centers (Alaska: US/Canada coasts; Hawaii: Pacific). Not 'Information' bulletins."""
    ns = {"a": "http://www.w3.org/2005/Atom", "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#"}
    events = []
    for feed in ("PAAQ", "PHEB"):
        root = ET.fromstring(get(f"https://www.tsunami.gov/events/xml/{feed}Atom.xml"))
        for e in root.findall("a:entry", ns):
            summary = ET.tostring(e.find("a:summary", ns), encoding="unicode", method="text")
            cat = re.search(r"Category:\s*(\w+)", summary)
            if not cat or cat.group(1) not in ("Warning", "Advisory", "Watch", "Threat"):
                continue
            issued = parse_time(e.findtext("a:updated", "", ns))
            if hours_ago(issued) > 6:
                continue
            lat, lon = float(e.findtext("geo:lat", "0", ns)), float(e.findtext("geo:long", "0", ns))
            region = e.findtext("a:title", "", ns).strip()
            events.append(Event(f"tsunami-{e.findtext('a:id', '', ns)}", "nearby",
                                f"Tsunami {cat.group(1).lower()}",
                                f"{region}. Follow local officials. Source: NOAA.",
                                cells_within(lat, lon, 2000), issued))
    return events


# ---------- CloudKit ----------

def cloudkit_create(event):
    """Creates the Alert record. Returns True when written (or it was already there)."""
    path = f"/database/1/{CONTAINER}/{ENVIRONMENT}/public/records/modify"
    body = json.dumps({"operations": [{"operationType": "create", "record": {
        "recordType": "Alert", "recordName": event.key,
        "fields": {
            "kind": {"value": event.kind},
            "title": {"value": event.title},
            "body": {"value": event.body},
            "cells": {"value": event.cells, "type": "STRING_LIST"},
            "issued": {"value": int(event.issued.timestamp() * 1000), "type": "TIMESTAMP"},
        }}}]}, separators=(",", ":")).encode()
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
        cells = [args[args.index("--cell") + 1]] if "--cell" in args else ["global"]
        e = Event(f"test-{NOW:%Y%m%d%H%M%S}", kind, "Heliotide test alert",
                  "If you see this, alerts work.", cells, NOW)
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
