"""Constants for the Folding@home v8 integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "foldingathome_v8"

WEB_CONTROL_URL = "https://v8-5.foldingathome.org/machines"

# Seconds between reconnect attempts while the machine is off. A failed
# attempt on the LAN is cheap, so keep the gap short to notice a boot quickly.
RECONNECT_MIN = 5
RECONNECT_MAX = 15

# Coalesce bursts of client updates into one entity refresh.
UPDATE_DEBOUNCE = 1.0

# The status sensor writes its unit details (progress) at most this often; a
# change of status, pause reason or work units is written right away.
STATUS_DETAILS_INTERVAL = timedelta(minutes=1)

# Donor statistics change when a work unit is credited; no need to poll often.
STATS_INTERVAL = timedelta(minutes=30)

# The stats API stalls for a moment every three hours (stats update). Keep the
# last values through this many failed polls in a row before going unavailable.
STATS_TOLERATED_FAILURES = 2

# Donor name and team learned from the client, kept for when it is off.
CONF_USER = "user"
CONF_TEAM = "team"
