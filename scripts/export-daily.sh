#!/usr/bin/env bash
#
# Export daily WorkBot outcome counts to a CSV (day, event, count), ready to
# feed a graph. Days are grouped by the UTC calendar date of each timestamp.
#
# Usage: ./export-daily.sh [output.csv]
#
set -euo pipefail

# Resolve the database relative to this script, not the caller's directory.
DB="$(cd "$(dirname "$0")/.." && pwd)/data/workbot.db"
OUT="${1:-events-daily-$(date +%Y%m%d).csv}"

# Clear errors beat cryptic sqlite output.
command -v sqlite3 >/dev/null || { echo "sqlite3 is not installed" >&2; exit 1; }
[ -f "$DB" ] || { echo "database not found: $DB" >&2; exit 1; }

# One row per (day, outcome); safe to run against the live DB (WAL mode).
sqlite3 -header -csv "$DB" \
  "SELECT date(ts) AS day, event, COUNT(*) AS n
     FROM events
 GROUP BY day, event
 ORDER BY day, event;" > "$OUT"

echo "wrote $OUT"
