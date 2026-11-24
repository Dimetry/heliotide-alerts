# heliotide-alerts

Scheduled checker for the Heliotide iOS app (space weather and nearby hazards).

Every 5 minutes GitHub Actions runs `checker.py`. It reads official sources and writes one
`Alert` record per new important event to the app's CloudKit public database. Phones that
turned on "Notify me" subscribe to these records; Apple delivers the push.

| Kind | Source | Sent when | Zones (km, chosen in the app) |
|---|---|---|---|
| storm | NOAA SWPC alerts | Kp 5+ reached; level = G1–G5, once per level per day | everyone |
| flare | NOAA SWPC alerts | X-rays above M5 (level 1), X-class (level 2) | everyone |
| radiation | NOAA SWPC alerts | proton event, level = S1–S3 | everyone |
| wind | NOAA SWPC summary | solar wind 600+ km/s, once per day | everyone |
| aurora | NOAA OVATION | during a storm; zones by chance 50% / 30% / 10% | that area |
| quake | USGS | M5.5+; level = magnitude | 300 / 1000 / 2000 |
| tsunami | NOAA tsunami centers | watch (1), advisory/threat (2), warning (3) | 500 / 1000 / 2000 |
| cyclone | GDACS | orange (2) / red (3) | 300 / 600 / 1000 |
| flood, volcano | GDACS | orange (2) / red (3) | 100 / 300 / 600 |
| fire | GDACS | orange (2) / red (3) | 50 / 100 / 300 |

The phone subscribes to `kind == X AND level >= its minimum AND zoneN CONTAINS its cell`.

Areas are 1° × 1° grid cells (about 110 km). The phone shares only its cell, never its position.

Chat moderation: `moderator.py` (workflow "Moderate chat", about every 30 minutes) deletes messages reported by
3+ people, bans for 7 days after 3 such messages in 7 days, deletes chat records older than 30 days and
"here now" marks older than 1 day. Its log holds counts only.

No user data is stored here. `state.json` lists events already sent, so nothing is sent twice.
Secrets (`CK_KEY_ID`, `CK_PRIVATE_KEY`) live in GitHub Actions secrets.

Run by hand: Actions → Check alerts → Run workflow (optionally a test kind).
Data: NOAA SWPC, USGS, GDACS (EU/UN), NOAA NTWC/PTWC — all public.
