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

import puzzles

HERE = os.path.dirname(os.path.abspath(__file__))
PARK = json.load(open(os.path.join(HERE, "data", "park.json")))
CANDS = PARK["candidates"]
WALK = PARK["walk"]
NODES = PARK["nodes"]

# How hard each landmark is to find from a clue, 1 (famous) to 5 (a plaque
# nobody knows). Written once by rate_landmarks.py, editable by hand.
_DIFF_FILE = os.path.join(HERE, "data", "landmark-difficulty.json")
_DIFF = json.load(open(_DIFF_FILE)) if os.path.exists(_DIFF_FILE) else {}
DIFFICULTY = [(_DIFF.get(c.get("name"), {}).get("rating", 3) if c["type"] == "landmark" else 3) for c in CANDS]
DIFF_SPREAD = 1   # most the teams' total difficulty may differ by

ADJ = [[] for _ in NODES]
FOOT_ADJ = [[] for _ in NODES]   # walking paths only: no road, no crossing, no sidewalk
ROAD_NODES = set()
for a, b, w, *flag in PARK["edges"]:
    ADJ[a].append((b, w))
    ADJ[b].append((a, w))
    if flag and flag[0]:
        FOOT_ADJ[a].append((b, w))
        FOOT_ADJ[b].append((a, w))
    else:
        ROAD_NODES.update((a, b))
# Path points not touching any road: where a Pace it out target may sit.
FOOT_NODES = {i for i in range(len(NODES)) if FOOT_ADJ[i] and i not in ROAD_NODES}

# Road points bucketed by ~50 m cells, for "how close is the nearest road".
_ROAD_GRID = {}
for _i in ROAD_NODES:
    _ROAD_GRID.setdefault((int(NODES[_i][0] / 0.00045), int(NODES[_i][1] / 0.00057)), []).append(_i)


def near_road(p, within):
    cy, cx = int(p[0] / 0.00045), int(p[1] / 0.00057)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for i in _ROAD_GRID.get((cy + dy, cx + dx), ()):
                if meters(p, NODES[i]) < within:
                    return True
    return False

ALL_TEAMS = ("red", "blue", "yellow")   # a game uses the first 2 or 3
TEAMS = ALL_TEAMS

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
            if any(meters(pt(c), pt(CANDS[t])) < MIN_STOP_GAP for t in list(taken) + chain + [start, final]):
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


def plan_routes(minutes, seed=None, teams=2):
    """One route per team from a shared start to a shared final spot, each
    with its own stops.

    Many random sets are drawn and the fairest is kept: the same number of
    stops in the same order of types, total walks within FAIR_SPREAD of each
    other, and similar last legs so no team gets a short sprint at the end.
    """
    rng = random.Random(seed)
    names = ALL_TEAMS[:teams]
    n = stops_for(minutes)
    target = minutes * METERS_PER_MINUTE
    leg = target / (n + 1)
    # Every stop is a landmark (Ray, 2026-10-15). Hidden spots on paths are
    # still in park.json; switching them back on is this one line.
    pattern = ["landmark"] * n

    starts = [i for i, c in enumerate(CANDS) if c["type"] == "landmark" and c["kind"] in START_KINDS]
    finals = [i for i, c in enumerate(CANDS) if c["type"] == "landmark" and c["kind"] in FINAL_KINDS]

    best, best_score = None, 1e18
    relaxed, relaxed_score = None, 1e18
    found = 0
    for _ in range(12000):
        start = rng.choice(starts)
        final = rng.choice(finals)
        if final == start or not (0.2 * target <= WALK[start][final] <= 0.55 * target):
            continue
        chains, taken = [], set()
        for _t in names:
            ch = _chain(rng, start, final, n, leg, pattern, taken)
            if not ch:
                break
            chains.append(ch)
            taken |= set(ch)
        if len(chains) < len(names):
            continue
        totals = [_total(start, ch, final) for ch in chains]
        mean = sum(totals) / len(totals)
        spread = (max(totals) - min(totals)) / mean
        lasts = [WALK[ch[-1]][final] for ch in chains]
        last_spread = (max(lasts) - min(lasts)) / max(lasts)
        # Total difficulty per team, so nobody gets all the famous spots
        # while another team hunts for plaques.
        diffs = [sum(DIFFICULTY[i] for i in ch) for ch in chains]
        diff_spread = max(diffs) - min(diffs)
        if spread > FAIR_SPREAD or last_spread > 0.3:
            continue
        if diff_spread > DIFF_SPREAD:
            # Fair on distance but not on difficulty: kept only as a fallback,
            # so a rare long three-team game still gets routes.
            loose = spread * 3 + diff_spread
            if loose < relaxed_score:
                relaxed, relaxed_score = (start, final, chains, totals, diffs), loose
            continue
        score = spread * 3 + abs(mean - target) / target + last_spread + diff_spread * 0.2
        found += 1
        if score < best_score:
            best, best_score = (start, final, chains, totals, diffs), score
        if found >= 40:
            break
    best = best or relaxed
    if not best:
        raise RuntimeError("Could not find fair routes")
    start, final, chains, totals, diffs = best
    return {
        "start": start,
        "final": final,
        "routes": {t: ch for t, ch in zip(names, chains)},
        "walk": {t: w for t, w in zip(names, totals)},
        "difficulty": {t: d for t, d in zip(names, diffs)},
        "target": target,
        "paths": {t: route_path([start] + ch + [final]) for t, ch in zip(names, chains)},
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

def new_game(minutes, seed=None, teams=2, mode="gm"):
    """mode "gm": a game master runs it from /gm. mode "self": no game
    master; any player starts it from the lobby, arriving is GPS only, and
    the game ends itself when the clock runs out."""
    plan = plan_routes(minutes, seed, teams)
    return {
        "mode": mode,
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
        "teams": {t: _fresh_team() for t in ALL_TEAMS[:teams]},
        "pending": {},               # team -> requested-at time for "We're here"
        "messages": [],              # from the game master, see send_message
        "events": [],
    }


def reroll(state, seed=None, plan=None):
    state["plan"] = plan or plan_routes(state["minutes"], seed, len(state["teams"]))
    state["riddles"] = {}
    state["riddles_status"] = "writing"


def set_team_count(state, n, plan):
    """Lobby only. Routes are planned together for fairness, so a new team
    count means a fresh plan. Anyone on a team that goes away moves to the
    smallest remaining team."""
    state["teams"] = {t: state["teams"].get(t) or _fresh_team() for t in ALL_TEAMS[:n]}
    for p in state["players"].values():
        if p["team"] not in state["teams"]:
            sizes = {t: sum(q["team"] == t for q in state["players"].values()) for t in state["teams"]}
            p["team"] = min(sizes, key=sizes.get)
    reroll(state, plan=plan)
    log(state, f"{ALL_TEAMS[2].capitalize()} team {'added' if n == 3 else 'removed'}")


def switch_team(state, token, team):
    """Lobby only: anyone can move to any team before the start."""
    p = state["players"][token]
    if p["team"] != team:
        p["team"] = team
        log(state, f"{p['name']} switched to {team.capitalize()}", team)


def shuffle_teams(state, who):
    """Deal everyone out evenly at random. Sizes differ by one at most."""
    tokens = list(state["players"])
    random.shuffle(tokens)
    teams = list(state["teams"])
    random.shuffle(teams)
    for i, k in enumerate(tokens):
        state["players"][k]["team"] = teams[i % len(teams)]
    log(state, f"{who} shuffled the teams")


def _fresh_team():
    return {"index": 0, "since": None, "asked_at": None, "mark": None, "answers": [],
            "challenge": None, "fails": 0, "locked_until": None}


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
    # Mind Meld and Switchboard need two people. With any one-player team
    # they are left out of the draw for everyone, without saying so.
    solo = any(sum(1 for p in state["players"].values() if p["team"] == t) < 2 for t in state["teams"])
    state["ladder"] = puzzles.plan_ladder(stops_for(state["minutes"]), random.Random(), solo)
    state["status"] = "running"
    state["started"] = now
    for t in list(state["teams"]):
        state["teams"][t] = _fresh_team()
        state["teams"][t]["since"] = now
    state["pending"] = {}
    sp = CANDS[state["plan"]["start"]]
    state["replay"] = {t: [[0, sp["lat"], sp["lon"]]] for t in state["teams"]}
    log(state, "Game started")


def arrive(state, team, how):
    """A team got to its current stop. The final spot wins the game; any
    other stop opens a challenge, and the next clue waits until it is solved."""
    tm = state["teams"][team]
    stops = stop_list(state, team)
    i = tm["index"]
    state["pending"].pop(team, None)
    label = team.capitalize()
    # For the replay at the end: when each stop was reached, and where.
    if i < len(stops):
        c = CANDS[stops[i]]
        tm.setdefault("hits", []).append([round(time.time() - (state.get("started") or time.time())), c["lat"], c["lon"]])
    if i == len(stops) - 1:
        tm["index"] += 1
        state["status"] = "finished"
        state["finished_at"] = time.time()
        state["winner"] = team
        state["pending"] = {}
        log(state, f"{label} reached the final spot and won", team)
        return
    if tm.get("challenge"):
        return
    tm["fails"], tm["locked_until"] = 0, None
    tm["challenge"] = make_challenge(state, team)
    log(state, f"{label} reached checkpoint {i + 1}{' (approved)' if how == 'gm' else ''}. "
               f"Challenge: {puzzles.NAMES[tm['challenge']['kind']]}", team)


def make_challenge(state, team):
    tm = state["teams"][team]
    i = tm["index"]
    ladder = state.get("ladder")
    if ladder and i < len(ladder):
        kind, level = ladder[i]
    else:
        kind, level = puzzles.LADDER[i % len(puzzles.LADDER)], i // len(puzzles.LADDER)
    # Down to one active player since the draw: swap a two-person challenge.
    # A phone that went quiet for a minute (Google Maps in front, a pocket)
    # still counts; only ten minutes of silence means the player is gone.
    if kind in puzzles.TEAM_ONLY and len(active_members(state, team, window=600)) < 2:
        kind = "lock"
    if tm["fails"] >= puzzles.EASIER_AFTER:
        level = -1
    stop = CANDS[stop_list(state, team)[i]]
    ctx = {
        "players": [player_id(p) for p in state["players"].values() if p["team"] == team],
        "names": [p["name"] for p in state["players"].values() if p["team"] == team],
        "landmark": stop["name"],
        "pace_target": lambda rng, lo, hi: pace_target(pt(stop), rng, lo, hi, stop["node"]),
    }
    return puzzles.new(kind, level, random.Random(), ctx)


def active_members(state, team, now=None, window=120):
    """Teammates whose phone has reported within the window, in seconds."""
    now = now or time.time()
    return [p for p in state["players"].values()
            if p["team"] == team and p.get("t") and now - p["t"] < window]


def meld_pair(state, team):
    """The two active teammates farthest apart, and how far apart they are."""
    ps = [p for p in active_members(state, team) if p.get("lat") is not None and p.get("acc", 99) <= 50]
    best, far = None, -1
    for x in range(len(ps)):
        for y in range(x + 1, len(ps)):
            d = meters((ps[x]["lat"], ps[x]["lon"]), (ps[y]["lat"], ps[y]["lon"]))
            if d > far:
                best, far = (ps[x], ps[y]), d
    return best, far


OUTLINE = [tuple(p) for p in PARK["outline"]]


def _in_park(p):
    y, x = p
    inside, j = False, len(OUTLINE) - 1
    for k in range(len(OUTLINE)):
        yi, xi = OUTLINE[k]
        yj, xj = OUTLINE[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = k
    return inside


PACE_ROAD_CLEAR = 20   # meters a Pace it out target keeps from any road


def pace_target(origin, rng, lo, hi, node=None):
    """A spot lo to hi meters from the landmark that a team can walk to
    safely:

    - on a walking path inside the park, so never in water and never in a
      fenced or paid area, since those have no paths in the data;
    - reached from the landmark along walking paths only, never along or
      across a road, and the walk not much longer than the straight line
      (that rules out the far side of a lake or a fence);
    - at least PACE_ROAD_CLEAR meters from any road.
    """
    start = None
    if node is not None:
        near = [i for i in FOOT_NODES if meters(origin, NODES[i]) <= 60]
        start = min(near, key=lambda i: meters(origin, NODES[i])) if near else None
    walk = _walk_from(start, hi * 2, FOOT_ADJ) if start is not None else {}

    def ok(i, a, b, strict=True):
        n = NODES[i]
        d = meters(origin, n)
        if not (a <= d <= b and _in_park(n)):
            return False
        if strict and (i not in FOOT_NODES or near_road(n, PACE_ROAD_CLEAR)):
            return False
        return start is None or walk.get(i, 1e18) <= 1.4 * d + 15

    pool = [i for i in walk if ok(i, lo, hi)] if start is not None else [i for i in FOOT_NODES if ok(i, lo, hi)]
    if not pool:
        pool = [i for i in walk if ok(i, lo / 2, hi * 1.5)]
    if not pool:
        pool = [i for i in FOOT_NODES if ok(i, lo / 2, hi * 1.5)]
    if not pool:
        # Hemmed in by roads: the nearest safe path spot at least 30 m off,
        # however far that is. Distance and direction in the text are real.
        safe = sorted((i for i in FOOT_NODES if not near_road(NODES[i], PACE_ROAD_CLEAR)
                       and meters(origin, NODES[i]) >= 30), key=lambda i: meters(origin, NODES[i]))
        pool = safe[:5]
    n = NODES[rng.choice(pool)]
    return (n[0], n[1]), int(round(meters(origin, n), -1)), bearing_word(origin, n)


def _walk_from(src, limit, adj=None):
    """Walking distance along paths from one node to everything within limit."""
    adj = adj or ADJ
    dist = {src: 0.0}
    heap = [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, 1e18) or d > limit:
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist


def advance(state, team, how):
    """The challenge is done: on to the next clue."""
    tm = state["teams"][team]
    i = tm["index"]
    stops = stop_list(state, team)
    tm["index"] += 1
    tm["since"] = time.time()
    tm["mark"] = None
    tm["answers"] = []
    tm["last_how"] = how
    state["pending"].pop(team, None)   # an old "ask the game master" must not approve the next stop
    tm["challenge"] = None
    tm["fails"], tm["locked_until"] = 0, None
    label = team.capitalize()
    note = " (passed by the game master)" if how == "gm" else ""
    if i == len(stops) - 2:
        log(state, f"{label} solved checkpoint {i + 1}{note}. Final spot unlocked", team)
    else:
        log(state, f"{label} solved checkpoint {i + 1}{note}", team)


def fail_challenge(state, team, why):
    """Out of tries, moves or time: wait it out, then a fresh challenge."""
    tm = state["teams"][team]
    now = time.time()
    tm["fails"] += 1
    tm["locked_until"] = now + puzzles.WAIT
    # What the failed one was, shown on the wait screen, so a team knows
    # what beat them instead of the board just vanishing.
    tm["last_fail"] = {"why": why, "reveal": puzzles.reveal(tm["challenge"]) if tm.get("challenge") else ""}
    tm["challenge"] = make_challenge(state, team)
    easier = " Easier one next" if tm["fails"] == puzzles.EASIER_AFTER else ""
    log(state, f"{team.capitalize()} failed the challenge ({why}). 5 minute wait.{easier}", team)


def challenge_action(state, token, action, value=None):
    """A player plays the team's challenge. Returns (ok, message)."""
    p = state["players"].get(token)
    if not p or state["status"] != "running":
        return False, "The game isn't running"
    team = p["team"]
    tm = state["teams"][team]
    ch = tm.get("challenge")
    if not ch:
        return False, "No challenge right now"
    now = time.time()
    if tm.get("locked_until") and now < tm["locked_until"]:
        return False, "Still waiting"
    if action == "start" and ch["kind"] == "meld" and ch["started"] is None:
        pair, apart = meld_pair(state, team)
        if not pair:
            return False, "Two of you need your location on"
        if apart < puzzles.MELD_APART:
            return False, f"You're {int(apart)} m apart. Get to {puzzles.MELD_APART} m"
        puzzles.start(ch, now)
        puzzles.meld_start(ch, now, [player_id(pair[0]), player_id(pair[1])], random.Random())
        log(state, f"{pair[0]['name']} and {pair[1]['name']} started Mind Meld", team)
        return True, ""
    if ch["kind"] == "clock" and action in ("start", "run"):
        if ch.get("run"):
            return True, ""
        if ch["started"] is None:
            puzzles.start(ch, now)
            log(state, f"{p['name']} started Inner Clock", team)
        elif ch["tries_left"] <= 0:
            return False, "No tries left"
        # Only people whose phone checked in during the last minute have to
        # tap: someone who locked their screen or left shouldn't sink the try.
        players = [player_id(q) for q in active_members(state, team, window=60)] or [player_id(p)]
        if player_id(p) not in players:
            players.append(player_id(p))
        puzzles.clock_run(ch, now, players, random.Random())
        return True, ""
    if action == "start":
        if puzzles.start(ch, now):
            log(state, f"{p['name']} started {puzzles.NAMES[ch['kind']]}", team)
        return True, ""
    if ch["started"] is None:
        return False, "Tap Start first"
    pid = player_id(p)
    if action == "guess" and ch["kind"] == "wordle":
        err = puzzles.guess_wordle(ch, value)
    elif action == "guess" and ch["kind"] == "lock":
        err = puzzles.guess_lock(ch, value)
    elif action == "press" and ch["kind"] == "switch":
        active = {player_id(q) for q in state["players"].values()
                  if q["team"] == team and q.get("t") and now - q["t"] < 90}
        err = puzzles.press_switch(ch, int(value), pid, active)
    elif action == "reset" and ch["kind"] == "switch":
        puzzles.reset_switch(ch)
        err = None
    elif action == "bet" and ch["kind"] == "highlow":
        res = puzzles.bet_highlow(ch, value)
        if res == "bust":
            # A wrong call is a short wait, not a new challenge: banked
            # points stay, and the clock pauses while they wait.
            tm["locked_until"] = now + puzzles.HIGHLOW_WAIT
            ch["deadline"] += puzzles.HIGHLOW_WAIT
            log(state, f"{team.capitalize()} lost a High/Low bet. 3 minute wait", team)
            return True, "Wrong call"
        err = None if res == "win" else res
    elif action == "bank" and ch["kind"] == "highlow":
        err = puzzles.bank_highlow(ch)
    elif action == "flip" and ch["kind"] == "tiles":
        err = puzzles.flip_tile(ch, int(value), random.Random())
    elif action == "answer" and ch["kind"] == "meld":
        err = puzzles.meld_answer(ch, pid, value, now)
    elif action == "tap" and ch["kind"] == "clock":
        value = value or {}
        err = puzzles.clock_tap(ch, pid, value.get("elapsed"), value.get("hidden"), now)
        if not err:
            res = puzzles.clock_tick(ch, now, {player_id(q): q["name"] for q in state["players"].values()})
            if res == "done":
                advance(state, team, "solved")
                return True, "Solved"
            if res == "failed":
                fail_challenge(state, team, "missed the time three times")
                return True, "Out of tries"
    else:
        return False, "Not for this challenge"
    if err:
        return False, err
    if ch["done"]:
        advance(state, team, "solved")
        return True, "Solved"
    if puzzles.failed(ch):
        why = {"switch": "out of moves", "tiles": "hit a trap"}.get(ch["kind"], "out of tries")
        fail_challenge(state, team, why)
        return True, "Hit a trap" if ch["kind"] == "tiles" else "Out of tries"
    return True, ""


def finish_on_time(state):
    """The clock ran out with nobody at the final spot. Most stops wins; on a
    tie, the team that got to its last stop first."""
    ranked = sorted(state["teams"].items(), key=lambda kv: (-kv[1]["index"], kv[1]["since"] or 0))
    top, second = ranked[0][1], ranked[1][1]
    state["status"] = "finished"
    state["finished_at"] = time.time()
    state["ended_on_time"] = True
    state["pending"] = {}
    if top["index"] == second["index"] and (top["since"] or 0) == (second["since"] or 0):
        state["winner"] = None
        log(state, f"Time's up. It's a tie at {top['index']} checkpoints")
        return
    winner = ranked[0][0]
    state["winner"] = winner
    log(state, f"Time's up. {winner.capitalize()} won with {top['index']} checkpoints", winner)


def tick(state):
    """Run out the clock on challenges. Returns True if anything changed."""
    if state["status"] != "running":
        return False
    now = time.time()
    changed = False
    if state.get("mode") == "self" and state["started"] and now > state["started"] + state["minutes"] * 60:
        finish_on_time(state)
        return True
    for team, tm in state["teams"].items():
        ch = tm.get("challenge")
        if ch and ch["kind"] == "meld" and not ch["done"]:
            res = puzzles.meld_tick(ch, now)
            if res == "done":
                advance(state, team, "solved")
            elif res == "failed":
                fail_challenge(state, team, "not enough matches")
            if res:
                changed = True
                continue
        if ch and ch["kind"] == "clock" and not ch["done"]:
            res = puzzles.clock_tick(ch, now, {player_id(q): q["name"] for q in state["players"].values()})
            if res == "done":
                advance(state, team, "solved")
            elif res == "failed":
                fail_challenge(state, team, "missed the time three times")
            if res:
                changed = True
                continue
        if ch and ch["deadline"] and now > ch["deadline"] and not ch["done"]:
            fail_challenge(state, team, "time ran out")
            changed = True
    return changed


def pass_challenge(state, team):
    """Game master override, for a stuck team or a broken puzzle."""
    if state["teams"][team].get("challenge"):
        advance(state, team, "gm")


def player_id(p):
    """A short public id for a player. The join token stays secret, so the
    game master's screen addresses players by this instead."""
    if "pid" not in p:
        p["pid"] = secrets.token_hex(4)
    return p["pid"]


def send_message(state, to, text):
    """A message from the game master to everyone, a team, or one player.

    to is {"kind": "all"} or {"kind": "team", "team": "red"} or
    {"kind": "player", "pid": "..."}. Returns an error string or None.
    """
    text = (text or "").strip()[:280]
    if not text:
        return "Write a message first"
    kind = to.get("kind")
    if kind == "team" and to.get("team") in state["teams"]:
        label = f"{to['team'].capitalize()} team"
        target = {"kind": "team", "team": to["team"]}
    elif kind == "player":
        match = next((p for p in state["players"].values() if player_id(p) == to.get("pid")), None)
        if not match:
            return "That player isn't in the game"
        label = match["name"]
        target = {"kind": "player", "pid": match["pid"], "team": match["team"]}
    elif kind == "team":
        return "No such team"
    else:
        label = "Everyone"
        target = {"kind": "all"}
    msgs = state.setdefault("messages", [])
    msgs.append({"id": (msgs[-1]["id"] + 1) if msgs else 1, "t": time.time(), "to": target,
                 "label": label, "text": text})
    del msgs[:-100]
    log(state, f"Message to {label}: {text}", target.get("team"))
    return None


def messages_for(state, p):
    out = []
    for m in state.get("messages", []):
        to = m["to"]
        if to["kind"] == "all":
            scope = "Everyone"
        elif to["kind"] == "team" and to["team"] == p["team"]:
            scope = "Your team"
        elif to["kind"] == "player" and to["pid"] == player_id(p):
            scope = "Just you"
        else:
            continue
        out.append({"id": m["id"], "t": m["t"], "scope": scope, "text": m["text"]})
    return out


def _num(x):
    """A finite float, or None."""
    if isinstance(x, bool):
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def record_fix(state, token, fix):
    """Store one position fix. Returns True if it finished a stop."""
    p = state["players"].get(token)
    if not p or not isinstance(fix, dict):
        return False
    # Everything from a phone is checked before it is kept: one bad number
    # here used to reach the once-a-second clock and stop it for every game.
    lat, lon, acc = _num(fix.get("lat")), _num(fix.get("lon")), _num(fix.get("acc"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    now = time.time()
    t = _num(fix.get("t"))
    # The phone's own time is used for queued fixes sent late, but never one
    # from the future or from long ago.
    if t is None or t > now + 5 or t < now - 600:
        t = now
    p["lat"], p["lon"], p["acc"] = lat, lon, acc if acc is not None and acc >= 0 else 99
    p["t"] = max(t, p.get("t") or 0)
    trail = p.setdefault("trail", [])
    if not trail or meters(trail[-1], (p["lat"], p["lon"])) > 8:
        trail.append([round(p["lat"], 6), round(p["lon"], 6)])
        del trail[:-150]
    if state["status"] != "running":
        return False
    # The replay path: one point per team every few seconds of walking,
    # timed from the start, kept for the whole game.
    rp = state.setdefault("replay", {}).setdefault(p["team"], [])
    rel = round(t - (state.get("started") or t))
    if not rp or (rel - rp[-1][0] >= 5 and meters(rp[-1][1:], (lat, lon)) > 15):
        if len(rp) < 4000:
            rp.append([rel, round(lat, 6), round(lon, 6)])
    stop = current_stop(state, p["team"])
    if stop is None:
        return False
    tm = state["teams"][p["team"]]
    ch = tm.get("challenge")
    if ch:
        # At a stop already. Only Pace it out cares where people are.
        now = time.time()
        if (ch["kind"] == "pace" and ch["started"] and not (tm.get("locked_until") and now < tm["locked_until"])
                and puzzles.pace_fix(ch, player_id(p), (p["lat"], p["lon"]), p["acc"], now, meters)):
            advance(state, p["team"], "solved")
            return True
        return False
    # Arrival is not automatic: a team checks in (check_in) when they think
    # they are there, so wandering past a stop by luck does not count.
    return False


CHECKIN_LOCK = 60      # seconds the Check in key rests after a wrong check-in
CHECKIN_FRESH = 30     # seconds; an older position does not count
TOGETHER_M = 60        # the whole team checks in together: everyone within this
TEAM_RECENT = 180      # seconds; a teammate silent longer than this doesn't hold the team up


def check_in(state, token, reading=None):
    """A player says their team is at the current stop. Their phone takes a
    fresh reading (the best of a few, sent as reading) and that decides it.
    Wrong means a one-minute rest for the whole team, and no hint about how
    close they were.

    GPS is the weak point, so this forgives honest error: the reading passes
    if it is within the stop's circle plus half its own reported accuracy."""
    if reading:
        record_fix(state, token, reading)
    p = state["players"].get(token)
    if not p or state["status"] != "running":
        return False, "The game isn't running"
    team = p["team"]
    tm = state["teams"][team]
    stop = current_stop(state, team)
    if stop is None:
        return False, "Nothing left to find"
    if tm.get("challenge"):
        return False, "You're already here"
    now = time.time()
    if tm.get("checkin_locked_until") and now < tm["checkin_locked_until"]:
        return False, "Try again in a moment"
    if p.get("lat") is None or not p.get("t") or now - p["t"] > CHECKIN_FRESH:
        return False, "Waiting for your location"
    if p.get("acc", 99) > GOOD_FIX:
        return False, "Your location is too rough right now. Step into the open and try again"
    # Teams travel together (Ray, 2026-10-16): no splitting up to tap at
    # every landmark. Checked before the spot, so it never says whether the
    # spot was right, and it costs no rest.
    far = []
    for k, q in state["players"].items():
        if k == token or q["team"] != team or q.get("lat") is None or not q.get("t") or now - q["t"] > TEAM_RECENT:
            continue
        d = meters((p["lat"], p["lon"]), (q["lat"], q["lon"]))
        if d > TOGETHER_M + min(q.get("acc", 99), GOOD_FIX) / 2:
            far.append((q["name"], d))
    if far:
        if len(far) == 1:
            return False, f"Wait for your team. {far[0][0]} is {int(round(far[0][1], -1))} m away"
        return False, f"Wait for your team. {', '.join(n for n, _ in far[:-1])} and {far[-1][0]} are too far away"
    c = CANDS[stop]
    # A team that has been on this stop a long time is likely fighting bad
    # GPS, not lost: the circle grows by half after 15 minutes.
    radius = c["radius"] * (1.5 if tm["since"] and now - tm["since"] > 15 * 60 else 1)
    if meters((p["lat"], p["lon"]), pt(c)) <= radius + min(p.get("acc", 99), GOOD_FIX) / 2:
        arrive(state, team, "gps")
        return True, "Checked in"
    tm["checkin_locked_until"] = now + CHECKIN_LOCK
    log(state, f"{p['name']} checked {team.capitalize()} in at the wrong spot", team)
    return False, "Not here. Try again in 1 minute"


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
    hints = hints_for(i, state["riddles"].get(str(i)), _hint_avoid(state, i))
    return {"type": c["type"], "hints": hints if unlocked_until is None else hints[:unlocked_until]}


def _hint_avoid(state, i):
    """Landmarks a hint must never point at: the final spot, and every other
    stop on any team's route, so a hint can't give away someone's next stop."""
    plan = state["plan"]
    stops = {plan["final"]} | {x for r in plan["routes"].values() for x in r}
    stops.discard(i)
    return tuple(stops)


def player_view(state, token):
    p = state["players"].get(token)
    if not p:
        return None
    team = p["team"]
    plan = state["plan"]
    now = time.time()
    tm = state["teams"][team]
    stops = stop_list(state, team)
    view = {
        "role": "player",
        "mode": state.get("mode", "gm"),
        "ended_on_time": state.get("ended_on_time", False),
        "code": state["code"],
        "status": state["status"],
        "winner": state["winner"],
        "minutes": state["minutes"],
        "started": state["started"],
        "finished_at": state.get("finished_at"),
        "team_sizes": {t: sum(1 for q in state["players"].values() if q["team"] == t) for t in state["teams"]},
        "me": {"name": p["name"], "team": team},
        "start": {"name": CANDS[plan["start"]]["name"], "lat": CANDS[plan["start"]]["lat"],
                  "lon": CANDS[plan["start"]]["lon"]},
        "total": len(stops),
        "index": tm["index"],
        "race": [{"team": t, "index": x["index"]} for t, x in state["teams"].items()],
        "teammates": [{"name": q["name"], "lat": q.get("lat"), "lon": q.get("lon"), "t": q.get("t"),
                       "me": k == token}
                      for k, q in state["players"].items() if q["team"] == team],
        "pending": team in state["pending"],
        "checkin_locked_until": tm.get("checkin_locked_until") if tm.get("checkin_locked_until", 0) > now else None,
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
        view["stop"]["hint_total"] = len(steps) - 1
        view["stop"]["since"] = tm["since"]
    ch = tm.get("challenge")
    if ch and state["status"] == "running":
        c = puzzles.public(ch, player_id(p))
        c["fails"] = tm["fails"]
        c["last_fail"] = tm.get("last_fail")
        c["locked_until"] = tm["locked_until"] if tm.get("locked_until") and now < tm["locked_until"] else None
        names = {player_id(q): q["name"] for q in state["players"].values() if q["team"] == team}
        if ch["kind"] == "switch":
            c["owner_names"] = [names.get(o, "") if o else "" for o in ch["owners"]]
        c["stop_name"] = CANDS[stops[tm["index"]]]["name"]
        c["stop_kind"] = CANDS[stops[tm["index"]]]["kind"]
        if ch["kind"] == "meld":
            pair, apart = meld_pair(state, team)
            c["apart"] = int(apart) if pair else None
            c["need_apart"] = puzzles.MELD_APART
            c["pair_names"] = [names.get(x, "") for x in (ch.get("pair") or [])]
        view["challenge"] = c
    if state["status"] == "finished":
        f = CANDS[plan["final"]]
        view["final"] = {"name": f["name"], "lat": f["lat"], "lon": f["lon"]}
        if state.get("closed"):
            view["closed"] = True
            view.pop("final")
        else:
            view["replay"] = {t: {"path": state.get("replay", {}).get(t, []), "hits": x.get("hits", [])}
                              for t, x in state["teams"].items()}
    view["last_how"] = tm.get("last_how")
    if state["status"] == "lobby":
        view["roster"] = {t: [{"name": q["name"], "pid": player_id(q), "me": k == token}
                              for k, q in state["players"].items() if q["team"] == t] for t in state["teams"]}
        view["riddles_status"] = state["riddles_status"]
    view["messages"] = messages_for(state, p)
    view["events"] = [e for e in state["events"]
                      if (e["team"] in (None, team) or "reached" in e["text"]) and not e["text"].startswith("Message to")][-20:]
    return view


def gm_view(state):
    plan = state["plan"]
    now = time.time()

    def stop_info(i, idx, team):
        c = CANDS[i]
        return {"lat": c["lat"], "lon": c["lon"], "type": c["type"], "radius": c["radius"],
                "name": c.get("name") or "Path spot", "kind": c.get("kind"),
                "clue": hints_for(i, state["riddles"].get(str(i)), _hint_avoid(state, i))[0]["text"],
                "done": state["teams"][team]["index"] > idx}

    teams = {}
    for t in state["teams"]:
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
            "challenge": _gm_challenge(tm, now),
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
        "messages": state.get("messages", [])[-30:],
        # Trails are cut to the last 60 points here: the game master's phone
        # gets this view about twice a second over cellular.
        "players": [{"pid": player_id(p), "name": p["name"], "team": p["team"], "lat": p.get("lat"), "lon": p.get("lon"),
                     "acc": p.get("acc"), "t": p.get("t"), "trail": p.get("trail", [])[-60:],
                     "sim": p.get("sim", False)}
                    for p in state["players"].values()],
        "events": state["events"][-40:],
        "now": now,
    }


def _gm_challenge(tm, now):
    ch = tm.get("challenge")
    if not ch:
        return None
    locked = tm.get("locked_until") and now < tm["locked_until"]
    if ch["kind"] in ("highlow", "tiles", "meld", "clock"):
        progress = puzzles.answer_text(ch)
    elif ch["kind"] in ("wordle", "lock"):
        progress = f"{len(ch['guesses'])} of {ch['max']} tries"
    elif ch["kind"] == "switch":
        progress = f"{ch['moves']} of {ch['max']} moves"
    else:
        progress = "Someone is holding still" if ch.get("hold") else "Looking for the spot"
    return {"name": puzzles.NAMES[ch["kind"]], "answer": puzzles.answer_text(ch), "fails": tm["fails"],
            "phase": "waiting" if locked else "playing" if ch["started"] else "ready",
            "locked_until": tm["locked_until"] if locked else None, "deadline": ch["deadline"],
            "progress": progress, "level": ch["level"]}


def riddle_targets(state):
    plan = state["plan"]
    ids = {i for r in plan["routes"].values() for i in r} | {plan["final"]}
    return [i for i in ids if CANDS[i]["type"] == "landmark"]
