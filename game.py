"""Game rules: picking fair routes, clues and hints, questions, arrival.

Everything here is plain functions over a state dict, so server.py can hold
the lock and do the saving. No Flask in this file.
"""

import heapq
import json
import math
import os
import random
import secrets
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PARK = json.load(open(os.path.join(HERE, "data", "park.json")))
CANDS = PARK["candidates"]
WALK = PARK["walk"]
NODES = PARK["nodes"]

ADJ = [[] for _ in NODES]
for a, b, w in PARK["edges"]:
    ADJ[a].append((b, w))
    ADJ[b].append((a, w))

TEAMS = ("red", "blue")

# Pace while hunting, not while walking: it allows for stopping to think,
# asking questions, and walking the wrong way for a bit. Tune after a real
# round.
METERS_PER_MINUTE = 45
FAIR_SPREAD = 0.05          # teams' total walks within 5% of each other
MIN_STOP_GAP = 150          # meters between any two stops, across teams
# Minutes on a stop before each hint shows. Every ladder ends with the spot
# marked on the map, so a stuck team always gets there in the end; being
# stuck just costs time.
HINT_STEPS = {"landmark": [0, 8, 16, 24, 32], "quiet": [0, 10, 18, 26, 34]}
SEARCH_CIRCLE = {"landmark": 100, "quiet": 80}  # meters across the circle, radius is half
QUESTION_COOLDOWN = 3 * 60  # seconds, per team, any question
RADAR_SIZES = (100, 250, 500)
THERMO_MIN_WALK = 80        # meters from the mark before asking
GOOD_FIX = 50               # meters; worse fixes never count for arrival
HOLD_FIXES = 2              # in-range fixes in a row to count as arrived

START_KINDS = {"lake", "windmill", "museum", "garden", "meadow", "waterfall", "hilltop"}
FINAL_KINDS = START_KINDS | {"memorial", "monument", "ruins", "viewpoint", "bridge", "fountain"}

# The park's long axis, for "west end", "middle", "east end".
WEST, EAST = -122.5110, -122.4540
MIDLINE = 37.7693


def meters(a, b):
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(lat)
    return math.hypot(dx, dy)


def bearing_word(frm, to):
    dy = to[0] - frm[0]
    dx = (to[1] - frm[1]) * math.cos(math.radians(frm[0]))
    ang = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    words = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
    return words[int((ang + 22.5) // 45) % 8]


def pt(c):
    return (c["lat"], c["lon"])


def section(p):
    third = (p[1] - WEST) / (EAST - WEST)
    along = "west end" if third < 0.33 else "middle" if third < 0.66 else "east end"
    side = "north side" if p[0] > MIDLINE else "south side"
    if along == "middle":
        return f"{side} of the middle of the park"
    return f"{side} of the {along}"


def nearest_landmark(i, exclude=(), at_least=0):
    best, best_d = None, 1e18
    for j, c in enumerate(CANDS):
        if c["type"] != "landmark" or j == i or j in exclude:
            continue
        d = meters(pt(CANDS[i]), pt(c))
        if d < at_least:
            continue
        if d < best_d:
            best, best_d = j, d
    return best, best_d


# ---------------------------------------------------------------- routes

def stops_for(minutes):
    return max(3, 4 + round((minutes - 90) / 30))


def _chain(rng, start, final, n, leg, pattern, taken):
    cur, chain = start, []
    for k in range(n):
        want = pattern[k]
        left = n - k  # legs still to walk after this one, including to final
        options = []
        for j, c in enumerate(CANDS):
            if c["type"] != want or j in taken or j in chain or j in (start, final):
                continue
            d = WALK[cur][j]
            if not (0.6 * leg <= d <= 1.45 * leg):
                continue
            if WALK[j][final] > leg * left * 1.35:
                continue
            if any(meters(pt(c), pt(CANDS[t])) < MIN_STOP_GAP for t in list(taken) + chain):
                continue
            options.append(j)
        if not options:
            return None
        cur = rng.choice(options)
        chain.append(cur)
    return chain


def _total(start, chain, final):
    seq = [start] + chain + [final]
    return sum(WALK[a][b] for a, b in zip(seq, seq[1:]))


def plan_routes(minutes, seed=None):
    """Two routes from one start to one final spot, each with its own stops.

    Many random pairs are drawn and the fairest one is kept: same number of
    landmark and quiet stops in the same order, total walks within
    FAIR_SPREAD, and similar last legs so neither team gets a short sprint
    at the end.
    """
    rng = random.Random(seed)
    n = stops_for(minutes)
    target = minutes * METERS_PER_MINUTE
    leg = target / (n + 1)
    pattern = ["landmark" if k % 2 == 0 else "quiet" for k in range(n)]

    starts = [i for i, c in enumerate(CANDS) if c["type"] == "landmark" and c["kind"] in START_KINDS]
    finals = [i for i, c in enumerate(CANDS) if c["type"] == "landmark" and c["kind"] in FINAL_KINDS]

    best, best_score = None, 1e18
    found = 0
    for _ in range(4000):
        start = rng.choice(starts)
        final = rng.choice(finals)
        if final == start or not (0.2 * target <= WALK[start][final] <= 0.55 * target):
            continue
        a = _chain(rng, start, final, n, leg, pattern, set())
        if not a:
            continue
        b = _chain(rng, start, final, n, leg, pattern, set(a))
        if not b:
            continue
        ta, tb = _total(start, a, final), _total(start, b, final)
        mean = (ta + tb) / 2
        spread = abs(ta - tb) / mean
        last_a, last_b = WALK[a[-1]][final], WALK[b[-1]][final]
        last_spread = abs(last_a - last_b) / max(last_a, last_b)
        if spread > FAIR_SPREAD or last_spread > 0.3:
            continue
        score = spread * 3 + abs(mean - target) / target + last_spread
        found += 1
        if score < best_score:
            best, best_score = (start, final, a, b, ta, tb), score
        if found >= 40:
            break
    if not best:
        raise RuntimeError("Could not find a fair pair of routes")
    start, final, a, b, ta, tb = best
    return {
        "start": start,
        "final": final,
        "routes": {"red": a, "blue": b},
        "walk": {"red": ta, "blue": tb},
        "target": target,
        "paths": {t: route_path([start] + r + [final]) for t, r in (("red", a), ("blue", b))},
    }


def route_path(seq):
    out = []
    for x, y in zip(seq, seq[1:]):
        out.extend(_shortest(CANDS[x]["node"], CANDS[y]["node"]))
    return [NODES[i] for i in out]


def _shortest(src, dst):
    dist, prev = {src: 0.0}, {}
    heap = [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == dst:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, w in ADJ[u]:
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u
                heapq.heappush(heap, (nd, v))
    path, u = [dst], dst
    while u != src and u in prev:
        u = prev[u]
        path.append(u)
    return path[::-1]


# ---------------------------------------------------------------- clues

KIND_WORDS = {"artwork": "piece of public art", "attraction": "landmark", "hilltop": "hilltop"}

def hints_for(i, riddle=None, avoid=()):
    """The ladder of hints for one stop, first one shown on arrival.

    Each hint is {"text": ...}, and the last two also carry something to draw:
    a search circle the spot sits somewhere inside, then the spot itself.
    """
    c = CANDS[i]
    p = pt(c)
    near, _ = nearest_landmark(i, avoid)
    near_name = CANDS[near]["name"]
    # The distance hint measures from a landmark at least 80 m off, so it
    # narrows things down without walking a team straight onto the spot.
    far, far_d = nearest_landmark(i, avoid, at_least=80)
    far_name = CANDS[far]["name"]
    toward = f"About {round(far_d, -1):.0f} m {bearing_word(pt(CANDS[far]), p)} of {far_name}"

    # The circle is offset from the spot by a fixed random amount, so its
    # middle is not the answer.
    rng = random.Random(i)
    r = SEARCH_CIRCLE[c["type"]] / 2
    ang, off = rng.uniform(0, 2 * math.pi), r * rng.uniform(0.3, 0.6)
    cx = p[0] + off * math.cos(ang) / 111320
    cy = p[1] + off * math.sin(ang) / (111320 * math.cos(math.radians(p[0])))
    circle = {"text": f"It's within {int(r)} m of this point",
              "circle": {"lat": round(cx, 6), "lon": round(cy, 6), "r": r}}

    if c["type"] == "landmark":
        noun = KIND_WORDS.get(c["kind"], c["kind"])
        plain = f"{'An' if noun[0] in 'aeiou' else 'A'} {noun} on the {section(p)}"
        return [{"text": riddle or plain},
                {"text": plain if riddle else f"Within 300 m of {near_name}"},
                {"text": toward},
                circle,
                {"text": f"It's {c['name']}. Here's the exact spot", "pin": {"lat": c["lat"], "lon": c["lon"]}}]

    ground = "open grass" if c["radius"] <= 30 else "trees"
    water = any(CANDS[j]["kind"] == "lake" and meters(p, pt(CANDS[j])) < 150
                for j in range(len(CANDS)) if CANDS[j]["type"] == "landmark")
    extra = ", near water" if water else f", among {ground}"
    return [{"text": f"A spot on a path, {section(p)}{extra}"},
            {"text": f"Within 250 m of {near_name}"},
            {"text": toward},
            circle,
            {"text": "Here's the exact spot", "pin": {"lat": c["lat"], "lon": c["lon"]}}]


# ---------------------------------------------------------------- state

def new_game(minutes, seed=None):
    plan = plan_routes(minutes, seed)
    return {
        "code": secrets.token_hex(3).upper(),
        "gm_key": secrets.token_urlsafe(9),
        "minutes": minutes,
        "status": "lobby",           # lobby, running, finished
        "created": time.time(),
        "started": None,
        "winner": None,
        "plan": plan,
        "riddles": {},               # candidate index (str) -> riddle text
        "riddles_status": "writing",
        "players": {},
        "teams": {t: _fresh_team() for t in TEAMS},
        "pending": {},               # team -> requested-at time for "We're here"
        "events": [],
    }


def reroll(state, seed=None):
    state["plan"] = plan_routes(state["minutes"], seed)
    state["riddles"] = {}
    state["riddles_status"] = "writing"
    state["events"] = []


def _fresh_team():
    return {"index": 0, "since": None, "asked_at": None, "mark": None, "answers": []}


def stop_list(state, team):
    plan = state["plan"]
    return plan["routes"][team] + [plan["final"]]


def current_stop(state, team):
    stops = stop_list(state, team)
    i = state["teams"][team]["index"]
    return stops[i] if i < len(stops) else None


def log(state, text, team=None):
    state["events"].append({"t": time.time(), "team": team, "text": text})
    state["events"] = state["events"][-200:]


def start(state):
    now = time.time()
    state["status"] = "running"
    state["started"] = now
    for t in TEAMS:
        state["teams"][t] = _fresh_team()
        state["teams"][t]["since"] = now
    state["pending"] = {}
    log(state, "Game started")


def arrive(state, team, how):
    tm = state["teams"][team]
    stops = stop_list(state, team)
    i = tm["index"]
    now = time.time()
    tm["index"] += 1
    tm["since"] = now
    tm["mark"] = None
    tm["answers"] = []
    state["pending"].pop(team, None)
    label = team.capitalize()
    if i == len(stops) - 1:
        state["status"] = "finished"
        state["winner"] = team
        state["pending"] = {}
        log(state, f"{label} reached the final spot and won", team)
    elif i == len(stops) - 2:
        log(state, f"{label} reached stop {i + 1}. Final spot unlocked", team)
    else:
        log(state, f"{label} reached stop {i + 1}{' (approved)' if how == 'gm' else ''}", team)


def record_fix(state, token, fix):
    """Store one position fix. Returns True if it finished a stop."""
    p = state["players"].get(token)
    if not p:
        return False
    p["lat"], p["lon"], p["acc"] = fix["lat"], fix["lon"], fix.get("acc", 99)
    p["t"] = fix.get("t") or time.time()
    trail = p.setdefault("trail", [])
    if not trail or meters(trail[-1], (p["lat"], p["lon"])) > 8:
        trail.append([round(p["lat"], 6), round(p["lon"], 6)])
        del trail[:-150]
    if state["status"] != "running":
        return False
    stop = current_stop(state, p["team"])
    if stop is None:
        return False
    c = CANDS[stop]
    close = meters((p["lat"], p["lon"]), pt(c)) <= c["radius"] and p["acc"] <= GOOD_FIX
    p["hold"] = p.get("hold", 0) + 1 if close else 0
    if p["hold"] >= HOLD_FIXES:
        for q in state["players"].values():
            if q["team"] == p["team"]:
                q["hold"] = 0
        arrive(state, p["team"], "gps")
        return True
    return False


def ask(state, token, kind, size=None):
    """Answer a question from a player. Returns (ok, message)."""
    p = state["players"].get(token)
    if not p or state["status"] != "running":
        return False, "The game isn't running"
    if p.get("lat") is None:
        return False, "No location yet"
    team = p["team"]
    tm = state["teams"][team]
    stop = current_stop(state, team)
    if stop is None:
        return False, "Nothing left to find"
    here = (p["lat"], p["lon"])
    target = pt(CANDS[stop])
    now = time.time()

    if kind == "mark":
        tm["mark"] = [here[0], here[1]]
        return True, "Spot marked. Walk at least 80 m, then ask"

    if tm["asked_at"] and now - tm["asked_at"] < QUESTION_COOLDOWN:
        return False, "Questions are cooling down"

    if kind == "radar":
        size = int(size or 250)
        if size not in RADAR_SIZES:
            return False, "Pick 100, 250 or 500 m"
        yes = meters(here, target) <= size
        q, a = f"Within {size} m?", "Yes" if yes else "No"
    elif kind == "thermo":
        if not tm["mark"]:
            return False, "Mark a spot first"
        moved = meters(tm["mark"], here)
        if moved < THERMO_MIN_WALK:
            return False, f"Walk {THERMO_MIN_WALK - int(moved)} m more first"
        closer = meters(here, target) < meters(tm["mark"], target)
        q, a = "Hotter or colder?", "Hotter" if closer else "Colder"
        tm["mark"] = None
    else:
        return False, "Unknown question"

    tm["asked_at"] = now
    tm["answers"].append({"t": now, "q": q, "a": a, "by": p["name"],
                          "at": [round(here[0], 6), round(here[1], 6)], "size": size})
    log(state, f"{team.capitalize()} asked {q.lower()} {a}", team)
    return True, a


# ---------------------------------------------------------------- views

def _stop_view(state, i, unlocked_until=None):
    c = CANDS[i]
    hints = hints_for(i, state["riddles"].get(str(i)), (state["plan"]["final"],))
    return {"type": c["type"], "hints": hints if unlocked_until is None else hints[:unlocked_until]}


def player_view(state, token):
    p = state["players"].get(token)
    if not p:
        return None
    team = p["team"]
    other = "blue" if team == "red" else "red"
    plan = state["plan"]
    now = time.time()
    tm = state["teams"][team]
    stops = stop_list(state, team)
    view = {
        "role": "player",
        "code": state["code"],
        "status": state["status"],
        "winner": state["winner"],
        "minutes": state["minutes"],
        "started": state["started"],
        "me": {"name": p["name"], "team": team},
        "start": {"name": CANDS[plan["start"]]["name"], "lat": CANDS[plan["start"]]["lat"],
                  "lon": CANDS[plan["start"]]["lon"]},
        "total": len(stops),
        "index": tm["index"],
        "other": {"team": other, "index": state["teams"][other]["index"]},
        "teammates": [{"name": q["name"], "lat": q.get("lat"), "lon": q.get("lon"), "t": q.get("t"),
                       "me": k == token}
                      for k, q in state["players"].items() if q["team"] == team],
        "pending": team in state["pending"],
        "cooldown_until": (tm["asked_at"] + QUESTION_COOLDOWN) if tm["asked_at"] else None,
        "mark": tm["mark"],
        "answers": tm["answers"],
        "found": [{"lat": CANDS[s]["lat"], "lon": CANDS[s]["lon"],
                   "name": CANDS[s].get("name") or "Path spot"} for s in stops[:tm["index"]]],
        "now": now,
    }
    if state["status"] == "running" and tm["index"] < len(stops):
        i = stops[tm["index"]]
        steps = HINT_STEPS[CANDS[i]["type"]]
        elapsed = (now - tm["since"]) / 60
        shown = sum(1 for s in steps if elapsed >= s)
        nxt = next((s for s in steps if elapsed < s), None)
        view["stop"] = _stop_view(state, i, shown)
        view["stop"]["final"] = tm["index"] == len(stops) - 1
        view["stop"]["next_hint_at"] = tm["since"] + nxt * 60 if nxt is not None else None
    if state["status"] == "finished":
        f = CANDS[plan["final"]]
        view["final"] = {"name": f["name"], "lat": f["lat"], "lon": f["lon"]}
    view["events"] = [e for e in state["events"] if e["team"] in (None, team) or "reached" in e["text"]][-20:]
    return view


def gm_view(state):
    plan = state["plan"]
    now = time.time()

    def stop_info(i, idx, team):
        c = CANDS[i]
        return {"lat": c["lat"], "lon": c["lon"], "type": c["type"], "radius": c["radius"],
                "name": c.get("name") or "Path spot", "kind": c.get("kind"),
                "clue": hints_for(i, state["riddles"].get(str(i)), (state["plan"]["final"],))[0]["text"],
                "done": state["teams"][team]["index"] > idx}

    teams = {}
    for t in TEAMS:
        stops = stop_list(state, t)
        tm = state["teams"][t]
        teams[t] = {
            "index": tm["index"],
            "total": len(stops),
            "since": tm["since"],
            "walk": plan["walk"][t],
            "stops": [stop_info(s, k, t) for k, s in enumerate(stops)],
            "path": plan["paths"][t],
            "answers": tm["answers"],
            "pending": state["pending"].get(t),
        }
    s, f = CANDS[plan["start"]], CANDS[plan["final"]]
    return {
        "role": "gm",
        "code": state["code"],
        "status": state["status"],
        "winner": state["winner"],
        "minutes": state["minutes"],
        "started": state["started"],
        "target": plan["target"],
        "riddles_status": state["riddles_status"],
        "start": {"name": s["name"], "lat": s["lat"], "lon": s["lon"]},
        "final": {"name": f["name"], "lat": f["lat"], "lon": f["lon"]},
        "teams": teams,
        "players": [{"name": p["name"], "team": p["team"], "lat": p.get("lat"), "lon": p.get("lon"),
                     "acc": p.get("acc"), "t": p.get("t"), "trail": p.get("trail", []),
                     "sim": p.get("sim", False)}
                    for p in state["players"].values()],
        "events": state["events"][-40:],
        "now": now,
    }


def riddle_targets(state):
    plan = state["plan"]
    ids = set(plan["routes"]["red"] + plan["routes"]["blue"] + [plan["final"]])
    return [i for i in ids if CANDS[i]["type"] == "landmark"]
