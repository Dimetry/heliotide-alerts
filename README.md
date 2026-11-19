# heliotide-alerts

Scheduled checker for the Heliotide iOS app (space weather and nearby hazards).

Every 5 minutes GitHub Actions runs `checker.py`. It reads official sources and writes one
`Alert` record per new important event to the app's CloudKit public database. Phones that
turned on "Notify me" subscribe to these records; Apple delivers the push.

| Kind | Source | Rule | Who gets it |
|---|---|---|---|
| storm | NOAA SWPC alerts | Kp 5+ reached (G1+), once per level per day | everyone |
| flare | NOAA SWPC alerts | X-rays above M5, X-class flare summary | everyone |
| radiation | NOAA SWPC alerts | proton event S1+ | everyone |
| wind | NOAA SWPC summary | solar wind 600+ km/s, once per day | everyone |
| aurora | NOAA OVATION | during a storm, aurora chance 20%+ | that area |
| nearby | USGS | M5.5+ within 300 km, M6+ 1,000 km, M7+ 2,000 km | that area |
| nearby | GDACS | orange/red cyclone (1,000 km), flood, volcano (300 km), wildfire (200 km) | that area |
| nearby | NOAA tsunami centers | warning, advisory, watch, threat (2,000 km) | that area |

Areas are 5° × 5° grid cells (about 550 km). The phone shares only its cell, never its position.

No user data is stored here. `state.json` lists events already sent, so nothing is sent twice.
Secrets (`CK_KEY_ID`, `CK_PRIVATE_KEY`) live in GitHub Actions secrets.

Run by hand: Actions → Check alerts → Run workflow (optionally a test kind).
Data: NOAA SWPC, USGS, GDACS (EU/UN), NOAA NTWC/PTWC — all public.
