#!/usr/bin/env bash
# loop_due.sh [EVERY_MIN] [NOW_EPOCH]  (ENG-73, engine_helper 10-19)
# The gate of a scheduled fleet workflow. Its base tick is hourly (cron at minute M of every hour); a run goes on when
# the UTC hour is a multiple of EVERY_MIN/60, so EVERY_MIN must be a whole number of hours, 60 to 1440.
# The loop time is the one-line file .github/loop_every_min (the gate reads it: empty or missing = 360). A bad value is
# not refused: it counts as 360, with a note (opus_boost 58290).
#   exit 0 = due now (run the workflow); exit 1 = not due (skip).
# NOW_EPOCH is for the tests; the workflow leaves it out and gets the real clock.
E="${1:-360}"
NOW="${2:-$(date -u +%s)}"
case "$E" in
  ''|*[!0-9]*) echo "loop time '$E' is not whole minutes: using 360"; E=360 ;;
esac
if [ "$E" -lt 60 ] || [ "$E" -gt 1440 ] || [ $((E % 60)) -ne 0 ]; then
  echo "loop time $E is not a multiple of 60 from 60 to 1440: using 360"; E=360
fi
STEP=$((E / 60))
HOUR=$(( (NOW / 3600) % 24 ))
if [ $((HOUR % STEP)) -eq 0 ]; then echo "due: UTC hour $HOUR, every $STEP h"; exit 0; fi
echo "not due: UTC hour $HOUR, every $STEP h"; exit 1
