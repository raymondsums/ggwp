"""Turn a raw OpenStreetMap download of Golden Gate Park into data/park.json.

Run once, or again whenever the map data should be refreshed:

    curl ... > data/osm_raw.json   (see README)
    .venv/bin/python build_park.py

The output is everything the game needs to pick stops without a network:
the walkable path graph, the named places worth sending people to, the
quieter spots on paths for the stops you have to deduce, and the walking
distance between every pair of candidate stops, so a game can be generated
in well under a second.
"""

import heapq
import json
import math
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "osm_raw.json")
OUT = os.path.join(HERE, "data", "park.json")

# "Golden Gate Park" is also the name of parks in Ohio and Chicago, and the
# download pulled their bus routes in. Anything outside this box is dropped.
BBOX = (37.760, -122.5125, 37.7760, -122.4520)

# Paths a person can walk. Trunk roads (19th Ave, Crossover Drive) are left
# out so a route never walks down a freeway; the park's own drives stay in,
# since they are car-free or have sidewalks.
WALKABLE = {
    "footway", "path", "pedestrian", "track", "cycleway", "steps", "service",
    "living_street", "residential", "unclassified", "tertiary", "secondary",
    "bridleway", "primary",
}
QUIET = {"footway", "path", "track", "bridleway", "pedestrian"}

# How a named place reads in a clue, by tag. First match wins.
KINDS = [
    ("waterway", "waterfall", "waterfall"),
    ("man_made", "windmill", "windmill"),
    ("man_made", "bridge", "bridge"),
    ("tourism", "museum", "museum"),
    ("amenity", "planetarium", "museum"),
    ("tourism", "aquarium", "museum"),
    ("tourism", "viewpoint", "viewpoint"),
    ("natural", "peak", "hilltop"),
    ("natural", "water", "lake"),
    ("historic", "memorial", "memorial"),
    ("historic", "monument", "monument"),
    ("historic", "ruins", "ruins"),
    ("historic", "wayside_cross", "monument"),
    ("tourism", "artwork", "artwork"),
    ("amenity", "fountain", "fountain"),
    ("leisure", "garden", "garden"),
    ("landuse", "meadow", "meadow"),
    ("leisure", "playground", "playground"),
    ("tourism", "attraction", "attraction"),
    ("amenity", "boat_rental", "boathouse"),
    ("amenity", "cafe", "cafe"),
    ("amenity", "theatre", "stage"),
    ("tourism", "picnic_site", "picnic area"),
    ("leisure", "dog_park", "dog park"),
    ("amenity", "stable", "stables"),
]

# Places that are never a stop: fenced, paid, indoors, or a hazard.
EXCLUDE_NAMES = {"Bison Paddock", "Kezar Stadium", "Golden Gate Park Golf Course", "Penguins"}

ARRIVE_OPEN = 30     # meters, open ground
ARRIVE_COVER = 45    # meters, everywhere else (trees, buildings)
SPACING = 70         # meters between quiet candidate spots
LANDMARK_SNAP = 70   # a landmark further than this from any path is skipped
EDGE_CLEAR = 50      # meters a stop must sit inside the park's edge. The
                     # boundary runs down the sidewalks of Fulton, Lincoln and
                     # Stanyan, and 64 stops used to land on them: on the wall,
                     # half outside the park, and not obviously reachable.


def meters(a, b):
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(lat)
    return math.hypot(dx, dy)


def in_bbox(p):
    return BBOX[0] <= p[0] <= BBOX[2] and BBOX[1] <= p[1] <= BBOX[3]


def point_in_poly(p, poly):
    y, x = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def edge_distance(p, ring):
    """Meters from p to the nearest point on the ring's edge."""
    k = math.cos(math.radians(p[0]))
    best = 1e18
    for a, b in zip(ring, ring[1:]):
        ax, ay = (a[1] - p[1]) * 111320 * k, (a[0] - p[0]) * 111320
        bx, by = (b[1] - p[1]) * 111320 * k, (b[0] - p[0]) * 111320
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        t = 0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def geom(e):
    return [(g["lat"], g["lon"]) for g in e.get("geometry", []) if g]


def kind_of(tags):
    for key, value, kind in KINDS:
        if tags.get(key) == value:
            return kind
    return None


def dijkstra(adj, src):
    dist = {src: 0.0}
    heap = [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, 1e18):
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist


def main():
    elements = json.load(open(RAW))["elements"]

    park = None
    for e in elements:
        t = e.get("tags", {})
        if e["type"] == "way" and t.get("name") == "Golden Gate Park":
            g = geom(e)
            if g and in_bbox(g[0]) and (park is None or len(g) > len(park)):
                park = g

    # Areas a stop may not sit inside.
    blocked = []
    paid = []
    closed = []   # (element id, outline): paid or indoor, with nothing to find outside
    open_ground = []
    for e in elements:
        t = e.get("tags", {})
        g = geom(e)
        if e["type"] != "way" or len(g) < 4 or not in_bbox(g[0]):
            continue
        if (t.get("natural") == "water" or t.get("leisure") in ("golf_course", "stadium")
                or t.get("access") in ("private", "no") or t.get("name") in EXCLUDE_NAMES
                or t.get("building")):
            blocked.append(g)
        if t.get("fee") == "yes" or t.get("building"):
            closed.append((e["id"], g))
        if t.get("fee") == "yes":
            paid.append(g)
        if t.get("landuse") in ("meadow", "grass") or t.get("leisure") in ("pitch", "park") and t.get("name") != "Golden Gate Park":
            open_ground.append(g)

    # Path graph.
    index = {}
    coords = []
    adj = []
    kinds = []

    def node(osm_id, p):
        if osm_id not in index:
            index[osm_id] = len(coords)
            coords.append(p)
            adj.append([])
            kinds.append(set())
        return index[osm_id]

    for e in elements:
        t = e.get("tags", {})
        hw = t.get("highway")
        if e["type"] != "way" or hw not in WALKABLE or t.get("access") in ("private", "no") or t.get("foot") == "no":
            continue
        g = geom(e)
        ids = e.get("nodes", [])
        if len(g) != len(ids) or not any(in_bbox(p) for p in g):
            continue
        prev = None
        for osm_id, p in zip(ids, g):
            # Paths inside a paid garden are cut, so no route walks through
            # somewhere that charges at the gate or shuts at 4pm.
            if any(point_in_poly(p, z) for z in paid):
                prev = None
                continue
            i = node(osm_id, p)
            # A sidewalk is a street, not somewhere in the park.
            kinds[i].add("sidewalk" if t.get("footway") == "sidewalk" else hw)
            if prev is not None and prev != i:
                w = meters(coords[prev], coords[i])
                adj[prev].append((i, w))
                adj[i].append((prev, w))
            prev = i

    # Keep the largest connected piece; stray fragments cannot be routed to.
    seen = [False] * len(coords)
    best = []
    for s in range(len(coords)):
        if seen[s]:
            continue
        comp, stack = [], [s]
        seen[s] = True
        while stack:
            u = stack.pop()
            comp.append(u)
            for v, _ in adj[u]:
                if not seen[v]:
                    seen[v] = True
                    stack.append(v)
        if len(comp) > len(best):
            best = comp
    keep = set(best)

    def usable(i):
        p = coords[i]
        return i in keep and point_in_poly(p, park) and not any(point_in_poly(p, b) for b in blocked)

    def nearest(p, pool):
        best_i, best_d = None, 1e18
        for i in pool:
            d = meters(p, coords[i])
            if d < best_d:
                best_i, best_d = i, d
        return best_i, best_d

    in_park = [i for i in keep if usable(i)]
    # Where a stop may go: well inside the edge and not on a sidewalk. Routes
    # can still use the rest of the graph to get there.
    stoppable = [i for i in in_park
                 if "sidewalk" not in kinds[i] and edge_distance(coords[i], park) >= EDGE_CLEAR]

    # Named places.
    landmarks = {}
    for e in elements:
        t = e.get("tags", {})
        name = t.get("name")
        kind = kind_of(t)
        if not name or not kind or name in EXCLUDE_NAMES or "highway" in t:
            continue
        g = geom(e) if e["type"] != "node" else [(e["lat"], e["lon"])]
        if e["type"] == "relation":
            continue
        if not g or not point_in_poly(g[0], park):
            continue
        # Something inside a museum or a paid garden: the penguins, a statue
        # past the ticket desk. Players could stand outside and never see it.
        mid = (sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g))
        if any(eid != e["id"] and point_in_poly(mid, z) for eid, z in closed):
            continue
        # For an area, the closest path point to its edge, not its middle:
        # the middle of a lake is not somewhere to stand.
        best_i, best_d = None, 1e18
        for p in g[:: max(1, len(g) // 40)]:
            i, d = nearest(p, stoppable)
            if d < best_d:
                best_i, best_d = i, d
        if best_i is None or best_d > LANDMARK_SNAP:
            continue
        key = name.strip().lower()
        if key in landmarks:
            continue
        landmarks[key] = {"name": name.strip(), "kind": kind, "node": best_i}

    landmark_nodes = [l["node"] for l in landmarks.values()]

    # Quiet spots: on a footpath, away from every named place, spread out.
    pool = [i for i in stoppable if kinds[i] & QUIET]
    random.seed(7)
    random.shuffle(pool)
    quiet = []
    for i in pool:
        p = coords[i]
        if any(meters(p, coords[j]) < 90 for j in landmark_nodes):
            continue
        if any(meters(p, coords[j]) < SPACING for j in quiet):
            continue
        quiet.append(i)

    # Candidate stops and walking distance between every pair.
    cand = []
    for l in landmarks.values():
        cand.append({"type": "landmark", "name": l["name"], "kind": l["kind"], "node": l["node"]})
    for i in quiet:
        cand.append({"type": "quiet", "node": i})
    for c in cand:
        p = coords[c["node"]]
        c["lat"], c["lon"] = round(p[0], 6), round(p[1], 6)
        c["radius"] = ARRIVE_OPEN if any(point_in_poly(p, g) for g in open_ground) else ARRIVE_COVER

    matrix = []
    for c in cand:
        dist = dijkstra(adj, c["node"])
        matrix.append([int(dist.get(d["node"], 10 ** 7)) for d in cand])

    # Compact graph for drawing routes: renumber kept nodes.
    order = sorted(keep)
    renum = {old: new for new, old in enumerate(order)}
    edges = []
    for u in order:
        for v, w in adj[u]:
            if v in renum and renum[u] < renum[v]:
                edges.append([renum[u], renum[v], round(w, 1)])
    for c in cand:
        c["node"] = renum[c["node"]]

    out = {
        "outline": [[round(a, 6), round(b, 6)] for a, b in park],
        "nodes": [[round(coords[i][0], 6), round(coords[i][1], 6)] for i in order],
        "edges": edges,
        "candidates": cand,
        "walk": matrix,
    }
    with open(OUT + ".tmp", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(OUT + ".tmp", OUT)
    print(f"{len(order)} path points, {len(edges)} edges, "
          f"{len(landmarks)} landmarks, {len(quiet)} quiet spots")


if __name__ == "__main__":
    main()
