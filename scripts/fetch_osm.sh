#!/bin/sh
# Download Golden Gate Park's paths and named places from OpenStreetMap into
# data/osm_raw.json. Then run: .venv/bin/python build_park.py
cd "$(dirname "$0")/.." || exit 1
cat > /tmp/parkrace-query.txt <<'Q'
[out:json][timeout:90];
way["leisure"="park"]["name"="Golden Gate Park"]->.p;
.p out geom;
.p map_to_area->.a;
(
  way["highway"~"^(footway|path|pedestrian|track|cycleway|steps|service|living_street|residential|unclassified|bridleway)$"](area.a);
);
out geom;
(
  nwr["name"](area.a);
  nwr["natural"="water"](area.a);
  nwr["leisure"~"golf_course|pitch|dog_park"](area.a);
  nwr["access"~"private|no"](area.a);
);
out geom;
Q
# Overpass turns away requests with no user agent (406).
curl -s -m 150 -A "parkrace/0.1 (personal project)" -H "Accept: application/json" \
  --data-urlencode data@/tmp/parkrace-query.txt https://overpass-api.de/api/interpreter \
  -o data/osm_raw.json && echo "Saved data/osm_raw.json"
