#!/usr/bin/env bash
# loop_due.sh EVERY_MIN [NOW_EPOCH]  (ENG-73, engine_helper 10-19)
# The gate of a scheduled fleet workflow. Its base tick is hourly (cron at minute M of every hour); a run goes on when
# the UTC hour is a multiple of EVERY_MIN/60, so EVERY_MIN must be a whole number of hours, 60 to 1440.
#   exit 0 = due now (run the workflow); exit 1 = not due (skip); exit 2 = EVERY_MIN is not allowed (skip, say so).
# NOW_EPOCH is for the tests; the workflow leaves it out and gets the real clock.
E="${1:-360}"
NOW="${2:-$(date -u +%s)}"
case "$E" in
  ''|*[!0-9]*) echo "EVERY_MIN must be whole minutes, got '$E'"; exit 2 ;;
esac
if [ "$E" -lt 60 ] || [ "$E" -gt 1440 ] || [ $((E % 60)) -ne 0 ]; then
  echo "EVERY_MIN $E must be a multiple of 60 from 60 to 1440 (the base tick is hourly)"; exit 2
fi
STEP=$((E / 60))
HOUR=$(( (NOW / 3600) % 24 ))
if [ $((HOUR % STEP)) -eq 0 ]; then echo "due: UTC hour $HOUR, every $STEP h"; exit 0; fi
echo "not due: UTC hour $HOUR, every $STEP h"; exit 1
