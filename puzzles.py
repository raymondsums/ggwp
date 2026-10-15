"""The challenge a team plays when it reaches a stop.

Reaching a stop opens a challenge; solving it unlocks the next clue. The
first stop is always Parkle. Every later stop draws from the pool, with no
repeats, sorted easier to harder (plan_ladder). All teams get the same draw.

    Parkle       Wordle with five guesses
    High/Low     bet whether the next card is higher; streaks pay more
    Safe tiles   a small Minesweeper: find enough safe tiles, hit no trap
    Lock code    crack a code from "right spot / wrong spot" feedback
    Mind Meld    two teammates 50 m apart pick the same answer in 10 seconds
    Switchboard  turn every light off; each phone owns some switches
    Pace it out  find a spot by distance and direction, hold still there
    Inner Clock  the timer hides; everyone taps at the target time, and the
                 team's average has to land close enough

Mind Meld and Switchboard need two players, so a game with any one-player
team leaves them out, and a team down to one active player mid-game gets
Lock code in their place.

Each team has one shared challenge. It sits on a start screen until someone
taps Start, then runs on a 10-minute clock. Failing (out of tries, out of
moves, or out of time) means a 5-minute wait, then a fresh challenge back on
the start screen. After three fails the team gets an easier version.

A challenge is a dict on the team. Its "secret" never leaves the server
except to the game master.
"""

import json
import math
import os
import random
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ANSWERS = [w.strip() for w in open(os.path.join(HERE, "data", "words-answers.txt")) if w.strip()]
ALLOWED = {w.strip() for w in open(os.path.join(HERE, "data", "words-allowed.txt")) if w.strip()}

LADDER = ["parkle", "lock", "switch", "pace"]   # old fixed order, kept for games saved before the pool
NAMES = {"parkle": "Parkle", "lock": "Lock code", "switch": "Switchboard", "pace": "Pace it out",
         "highlow": "High/Low", "tiles": "Safe tiles", "meld": "Mind Meld", "clock": "Inner Clock"}
# Pace it out was taken out of the draw (Ray, 2026-10-15): it leans on GPS
# more than anything else in the game. Its code stays; add "pace" back here
# to bring it back.
POOL = ["highlow", "tiles", "lock", "clock", "meld", "switch"]
RATING = {"parkle": 0, "highlow": 1, "tiles": 2, "lock": 2, "clock": 2, "meld": 3, "switch": 3, "pace": 4}
TEAM_ONLY = {"meld", "switch"}
MELD_BANK = json.load(open(os.path.join(HERE, "data", "meld-questions.json")))
TIME_LIMIT = 10 * 60     # seconds on the clock once Start is tapped
WAIT = 5 * 60            # seconds locked out after a fail
EASIER_AFTER = 3         # fails on one stop before the easier version

# What "harder" and "easier" mean for each kind. Level 0 is the first time
# round the ladder, 1 the second; -1 is the easier version after three fails.
PARKLE_GUESSES = {-1: 6, 0: 5, 1: 5}
LOCK = {-1: (3, 8), 0: (4, 8), 1: (5, 10)}            # digits, tries
SWITCH = {-1: (3, 3), 0: (3, 5), 1: (4, 7)}           # grid size, presses used to scramble
SWITCH_SPARE = 4                                      # moves allowed beyond the scramble
PACE = {-1: (40, 60), 0: (60, 90), 1: (100, 140)}     # meters from the landmark
PACE_RADIUS = 20         # meters around the target that count
PACE_HOLD = 10           # seconds a player has to stay inside
PACE_GOOD_FIX = 30       # meters; worse fixes do not count toward the hold
HIGHLOW_TARGET = {-1: 3, 0: 5, 1: 7}                  # points to clear
HIGHLOW_WAIT = 3 * 60    # a wrong call costs this, and keeps banked points
TILES = {-1: (4, 3, 6), 0: (5, 4, 8), 1: (6, 6, 12)}  # grid size, traps, safe tiles needed
MELD = {-1: (5, 2), 0: (5, 3), 1: (5, 4)}             # rounds, matches needed
MELD_ROUND = 10          # seconds to lock in an answer
MELD_REVEAL = 4          # seconds showing both answers before the next round
MELD_APART = 50          # meters the pair has to be apart to start
CLOCK = {-1: (1.5, 20, 35), 0: (1.0, 20, 45), 1: (0.6, 40, 75)}   # seconds of slack, target range
CLOCK_TRIES = 3
CLOCK_LEAD = 3           # 3-2-1 before the timer starts
CLOCK_SHOW = 5           # seconds the timer is visible before it hides


def plan_ladder(stops, rng, solo):
    """Which challenge each stop gets: (kind, level) per stop, the final
    excluded. Parkle first; then the pool shuffled, sorted by rating, and
    gone round again at level 1 if the game has more stops than the pool."""
    pool = [k for k in POOL if not (solo and k in TEAM_ONLY)]
    out = [("parkle", 0)]
    level = 0
    while len(out) < stops:
        batch = sorted(pool, key=lambda k: (RATING[k], rng.random()))
        rng.shuffle(batch)
        take = sorted(batch[:stops - len(out)], key=lambda k: (RATING[k], rng.random()))
        # Going round again: never the same challenge twice in a row.
        if take and take[0] == out[-1][0] and len(take) > 1:
            take[0], take[1] = take[1], take[0]
        out += [(k, level) for k in take]
        level = 1
    return out[:stops]


def new(kind, level, rng, ctx):
    """A fresh challenge. ctx carries what Pace it out and Switchboard need:
    the landmark's position and a way to snap to walkable ground, and the
    team's player ids."""
    lvl = max(-1, min(1, level))
    ch = {"kind": kind, "level": lvl, "started": None, "deadline": None, "done": False}
    if kind == "parkle":
        ch["secret"] = {"word": rng.choice(ANSWERS)}
        ch["guesses"] = []
        ch["max"] = PARKLE_GUESSES[lvl]
    elif kind == "lock":
        digits, tries = LOCK[lvl]
        ch["secret"] = {"code": "".join(rng.sample("0123456789", digits))}
        ch["digits"], ch["max"], ch["guesses"] = digits, tries, []
    elif kind == "switch":
        size, scramble = SWITCH[lvl]
        presses = rng.sample(range(size * size), scramble)
        lights = [0] * (size * size)
        for i in presses:
            _press(lights, size, i)
        if not any(lights):                       # scrambled back to dark; nudge it
            _press(lights, size, presses[0])
            presses = presses[1:]
        ch["size"], ch["start"], ch["lights"] = size, lights[:], lights
        ch["moves"], ch["max"] = 0, len(presses) + SWITCH_SPARE
        ch["owners"] = _owners(size, ctx.get("players", []))
        ch["secret"] = {"presses": presses}
    elif kind == "highlow":
        deck = list(range(1, 11))
        rng.shuffle(deck)
        ch["card"] = deck.pop()
        ch["secret"] = {"deck": deck}
        ch["score"], ch["pot"], ch["streak"], ch["target"], ch["last"] = 0, 0, 0, HIGHLOW_TARGET[lvl], None
    elif kind == "tiles":
        size, traps, need = TILES[lvl]
        ch["size"], ch["traps"], ch["need"] = size, traps, need
        ch["revealed"], ch["boom"] = {}, None
        ch["secret"] = {"traps": None}      # placed on the first flip, so it is never a trap
    elif kind == "meld":
        rounds, need = MELD[lvl]
        ch["rounds"], ch["need"] = rounds, need
        ch["secret"] = {"questions": _meld_questions(rng, rounds, ctx.get("names", []))}
        ch["pair"], ch["round"], ch["round_start"], ch["answers"], ch["results"] = None, 0, None, {}, []
        ch["order"] = {}
    elif kind == "clock":
        ch["buffer"], ch["tries_left"], ch["run"], ch["history"] = CLOCK[lvl][0], CLOCK_TRIES, None, []
        ch["secret"] = {}
    elif kind == "pace":
        lo, hi = PACE[lvl]
        target, dist, word = ctx["pace_target"](rng, lo, hi)
        ch["secret"] = {"lat": target[0], "lon": target[1]}
        ch["text"] = f"Find the spot about {dist} m {word} of {ctx['landmark']}, and hold still there for {PACE_HOLD} seconds"
        ch["hold"] = None   # {"pid", "since"} while someone is inside
    return ch


# ---------------------------------------------------------------- moves

def _meld_questions(rng, rounds, names):
    """Mixed types, one trivia question, no "about each other" unless there
    are names to use."""
    trivia = [q for q in MELD_BANK if q["type"] == "trivia"]
    others = [q for q in MELD_BANK if q["type"] != "trivia" and (q["options"] != "names" or len(names) >= 2)]
    picks = rng.sample(others, rounds - 1) + [rng.choice(trivia)]
    rng.shuffle(picks)
    out = []
    for q in picks:
        opts = list(names) if q["options"] == "names" else list(q["options"])
        out.append({"q": q["q"], "options": opts, "answer": q.get("answer"), "type": q["type"]})
    return out


def bet_highlow(ch, call):
    """call is "higher" or "lower". Returns "win", "bust" or an error."""
    if call not in ("higher", "lower"):
        return "Higher or lower"
    deck = ch["secret"]["deck"]
    if not deck:
        deck[:] = [c for c in range(1, 11) if c != ch["card"]]
        random.shuffle(deck)
    nxt = deck.pop()
    win = nxt > ch["card"] if call == "higher" else nxt < ch["card"]
    ch["last"] = {"card": ch["card"], "next": nxt, "call": call, "win": win}
    ch["card"] = nxt
    if win:
        ch["streak"] += 1
        ch["pot"] += ch["streak"]
        if ch["score"] + ch["pot"] >= ch["target"]:
            ch["score"] += ch["pot"]
            ch["pot"] = 0
            ch["done"] = True
        return "win"
    ch["pot"], ch["streak"] = 0, 0
    return "bust"


def bank_highlow(ch):
    if not ch["pot"]:
        return "Nothing to bank yet"
    ch["score"] += ch["pot"]
    ch["pot"], ch["streak"] = 0, 0
    if ch["score"] >= ch["target"]:
        ch["done"] = True
    return None


def flip_tile(ch, i, rng):
    size = ch["size"]
    if not (0 <= i < size * size):
        return "No such tile"
    if str(i) in ch["revealed"]:
        return "Already open"
    if ch["secret"]["traps"] is None:
        spots = [k for k in range(size * size) if k != i]
        ch["secret"]["traps"] = rng.sample(spots, ch["traps"])
    traps = ch["secret"]["traps"]
    if i in traps:
        ch["boom"] = i
        return None
    r, c = divmod(i, size)
    near = sum(1 for dr in (-1, 0, 1) for dc in (-1, 0, 1)
               if (dr or dc) and 0 <= r + dr < size and 0 <= c + dc < size and (r + dr) * size + c + dc in traps)
    ch["revealed"][str(i)] = near
    if len(ch["revealed"]) >= ch["need"]:
        ch["done"] = True
    return None


def meld_start(ch, now, pair, rng):
    ch["pair"] = pair
    ch["round"], ch["round_start"], ch["answers"], ch["results"] = 0, now + 3, {}, []
    qs = ch["secret"]["questions"]
    # Each phone sees the choices in its own order, so "always pick the top
    # one" is not a strategy.
    ch["order"] = {pid: [rng.sample(range(len(q["options"])), len(q["options"])) for q in qs] for pid in pair}


def meld_answer(ch, pid, choice, now):
    if pid not in (ch.get("pair") or []):
        return "Your teammates are playing this one"
    if ch["round"] >= ch["rounds"] or now < ch["round_start"] or now > ch["round_start"] + MELD_ROUND:
        return "Not open right now"
    if pid in ch["answers"]:
        return "Already locked in"
    q = ch["secret"]["questions"][ch["round"]]
    if not (0 <= int(choice) < len(q["options"])):
        return "Pick one"
    ch["answers"][pid] = int(choice)
    return None


def meld_tick(ch, now):
    """Close a round when both have answered or time is up. Returns "done",
    "failed", "changed" or None."""
    if not ch.get("pair") or ch["round"] >= ch["rounds"] or now < ch["round_start"]:
        return None
    a, b = ch["pair"]
    if not (len(ch["answers"]) == 2 or now > ch["round_start"] + MELD_ROUND):
        return None
    q = ch["secret"]["questions"][ch["round"]]
    x, y = ch["answers"].get(a), ch["answers"].get(b)
    match = x is not None and x == y and (q["answer"] is None or q["options"][x] == q["answer"])
    ch["results"].append({"q": q["q"], "a": q["options"][x] if x is not None else None,
                          "b": q["options"][y] if y is not None else None, "match": match,
                          "trivia": q["answer"] is not None})
    ch["round"] += 1
    ch["answers"] = {}
    ch["round_start"] = now + MELD_REVEAL
    matches = sum(r["match"] for r in ch["results"])
    if matches >= ch["need"]:
        ch["done"] = True
        return "done"
    if matches + (ch["rounds"] - ch["round"]) < ch["need"]:
        return "failed"
    return "changed"

def start(ch, now):
    if ch["started"] is None:
        ch["started"], ch["deadline"] = now, now + TIME_LIMIT
        return True
    return False


def guess_parkle(ch, word):
    word = (word or "").strip().lower()
    if len(word) != 5 or not word.isalpha():
        return "Five letters"
    if word not in ALLOWED:
        return "Not in the word list"
    answer = ch["secret"]["word"]
    ch["guesses"].append({"word": word, "marks": wordle_marks(word, answer)})
    if word == answer:
        ch["done"] = True
    return None


def wordle_marks(guess, answer):
    """g for right spot, y for in the word elsewhere, x for not in it, with
    repeated letters counted the way Wordle does."""
    marks = ["x"] * 5
    left = {}
    for i, (a, b) in enumerate(zip(guess, answer)):
        if a == b:
            marks[i] = "g"
        else:
            left[b] = left.get(b, 0) + 1
    for i, a in enumerate(guess):
        if marks[i] == "x" and left.get(a, 0):
            marks[i] = "y"
            left[a] -= 1
    return marks


def guess_lock(ch, code):
    code = (code or "").strip()
    if len(code) != ch["digits"] or not code.isdigit():
        return f"{ch['digits']} digits"
    if len(set(code)) != len(code):
        return "No repeated digits"
    secret = ch["secret"]["code"]
    right = sum(a == b for a, b in zip(code, secret))
    near = len(set(code) & set(secret)) - right
    ch["guesses"].append({"code": code, "right": right, "near": near})
    if code == secret:
        ch["done"] = True
    return None


def press_switch(ch, cell, pid, active_pids):
    size = ch["size"]
    if not (0 <= cell < size * size):
        return "No such switch"
    owner = ch["owners"][cell]
    # A switch whose owner has gone quiet can be pressed by anyone, so one
    # locked phone does not strand the team.
    if owner and owner != pid and owner in active_pids:
        return "That switch belongs to a teammate"
    _press(ch["lights"], size, cell)
    ch["moves"] += 1
    if not any(ch["lights"]):
        ch["done"] = True
    return None


def reset_switch(ch):
    """Back to the starting lights. Moves already used stay used."""
    ch["lights"] = ch["start"][:]


def _press(lights, size, i):
    r, c = divmod(i, size)
    for dr, dc in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
        rr, cc = r + dr, c + dc
        if 0 <= rr < size and 0 <= cc < size:
            lights[rr * size + cc] ^= 1


def _owners(size, pids):
    """Rows of switches dealt round the team. One player owns them all."""
    if len(pids) <= 1:
        return [None] * (size * size)
    return [pids[(i // size) % len(pids)] for i in range(size * size)]


def pace_fix(ch, pid, here, acc, now, meters):
    """Feed one position fix in. Returns True once someone has held still
    inside the target long enough."""
    inside = acc <= PACE_GOOD_FIX and meters(here, (ch["secret"]["lat"], ch["secret"]["lon"])) <= PACE_RADIUS
    hold = ch.get("hold")
    if inside:
        if not hold or hold["pid"] != pid:
            # Take over a hold whose owner has gone quiet.
            if not hold or now - hold.get("seen", hold["since"]) > 5:
                ch["hold"] = {"pid": pid, "since": now, "seen": now}
        elif hold:
            hold["seen"] = now
        if hold and hold["pid"] == pid and now - hold["since"] >= PACE_HOLD:
            ch["done"] = True
            return True
    elif hold and hold["pid"] == pid:
        ch["hold"] = None
    return False


def clock_run(ch, now, pids, rng):
    """Start one try: a fresh target, and everyone on the team taps."""
    _, lo, hi = CLOCK[ch["level"]]
    ch["run"] = {"start": now + CLOCK_LEAD, "target": rng.randint(lo, hi), "players": pids, "taps": {}}


def clock_tap(ch, pid, elapsed, hidden, now):
    run = ch.get("run")
    if not run or pid not in run["players"]:
        return "Not your turn"
    if pid in run["taps"]:
        return "Already tapped"
    server = now - run["start"]
    if server < 0:
        return "Not started"
    # The phone's own count is used, since it is not thrown off by the
    # network, unless it is far from what the server saw.
    try:
        elapsed = float(elapsed)
    except (TypeError, ValueError):
        elapsed = server
    if not math.isfinite(elapsed):
        elapsed = server
    if abs(elapsed - server) > 2.5:
        elapsed = server
    run["taps"][pid] = {"t": round(elapsed, 2), "hidden": bool(hidden)}
    return None


def clock_tick(ch, now, names):
    """Close a try when everyone has tapped, or 8 seconds after the target.
    Returns "done", "failed", "changed" or None."""
    run = ch.get("run")
    if not run:
        return None
    if not (len(run["taps"]) == len(run["players"]) or now > run["start"] + run["target"] + 8):
        return None
    taps = [run["taps"].get(p) for p in run["players"]]
    missing = any(t is None for t in taps)
    left_page = any(t and t["hidden"] for t in taps)
    avg = sum(t["t"] for t in taps if t) / max(1, sum(1 for t in taps if t))
    ok = not missing and not left_page and abs(avg - run["target"]) <= ch["buffer"]
    why = "someone didn't tap" if missing else "someone left the game screen" if left_page else None
    ch["history"].append({"target": run["target"], "avg": round(avg, 2), "ok": ok, "why": why,
                          "taps": [{"name": names.get(p, ""), "t": (run["taps"].get(p) or {}).get("t")} for p in run["players"]]})
    ch["run"] = None
    ch["tries_left"] -= 1
    if ok:
        ch["done"] = True
        return "done"
    if ch["tries_left"] <= 0:
        return "failed"
    return "changed"


def failed(ch):
    """Out of tries or moves. Running out of time is checked by the caller."""
    if ch["kind"] == "tiles":
        return ch["boom"] is not None
    if ch["kind"] in ("parkle", "lock"):
        return not ch["done"] and len(ch["guesses"]) >= ch["max"]
    if ch["kind"] == "switch":
        return not ch["done"] and ch["moves"] >= ch["max"]
    return False


# ---------------------------------------------------------------- views

def public(ch, pid=None):
    """What a player's phone gets. No answers."""
    out = {k: v for k, v in ch.items() if k not in ("secret", "start")}
    out["name"] = NAMES[ch["kind"]]
    if ch["kind"] == "switch":
        out["mine"] = [o is None or o == pid for o in ch["owners"]]
    if ch["kind"] == "meld":
        out.pop("order", None)
        out.pop("answers", None)    # only your own pick and a count go out
        qs = ch["secret"]["questions"]
        if ch.get("pair") and ch["round"] < ch["rounds"]:
            q = qs[ch["round"]]
            # Teammates watching (not in the pair) have no shuffled order of
            # their own; they see the choices as written.
            mine = ch["order"].get(pid)
            order = mine[ch["round"]] if mine else list(range(len(q["options"])))
            out["question"] = {"q": q["q"], "trivia": q["answer"] is not None,
                               "options": [{"i": i, "text": q["options"][i]} for i in order]}
            out["mine"] = ch["answers"].get(pid)
            out["answered"] = len(ch["answers"])
        out["playing"] = pid in (ch.get("pair") or [])
        out["round_ends"] = ch["round_start"] + MELD_ROUND if ch.get("round_start") else None
    if ch["kind"] == "clock" and ch.get("run"):
        run = ch["run"]
        out["run"] = {"start": run["start"], "target": run["target"], "count": len(run["players"]),
                      "tapped": len(run["taps"]), "mine": pid in run["taps"], "playing": pid in run["players"],
                      "show": CLOCK_SHOW}
    if ch["kind"] == "tiles" and ch["boom"] is not None:
        out["trap_spots"] = ch["secret"]["traps"]
    if ch["kind"] == "pace":
        out["holding"] = bool(ch.get("hold"))
        out["hold_since"] = ch["hold"]["since"] if ch.get("hold") else None
        out["hold_needed"] = PACE_HOLD
    return out


def reveal(ch):
    """What a failed challenge was, in plain words, for the wait screen."""
    s = ch.get("secret", {})
    k = ch["kind"]
    if k == "parkle":
        return f"The word was {s['word'].upper()}"
    if k == "lock":
        return f"The code was {s['code']}"
    if k == "tiles":
        return f"{len(ch['revealed'])} of {ch['need']} safe tiles opened"
    if k == "switch":
        return f"{sum(ch['lights'])} lights still on"
    if k == "highlow":
        last = ch.get("last")
        return f"Last draw was {last['next']}" if last else ""
    if k == "meld":
        return f"{sum(r['match'] for r in ch['results'])} of {ch['need']} matches"
    if k == "clock":
        h = ch["history"][-1] if ch.get("history") else None
        return f"Last try: target {h['target']} s, team average {h['avg']} s" if h else ""
    return ""


def answer_text(ch):
    """For the game master's screen."""
    s = ch["secret"]
    if ch["kind"] == "parkle":
        return s["word"].upper()
    if ch["kind"] == "lock":
        return s["code"]
    if ch["kind"] == "switch":
        return f"{len(s['presses'])} presses"
    if ch["kind"] == "highlow":
        return f"{ch['score']} banked, {ch['pot']} riding, of {ch['target']}"
    if ch["kind"] == "tiles":
        return f"{len(ch['revealed'])} of {ch['need']} safe tiles"
    if ch["kind"] == "meld":
        return f"{sum(r['match'] for r in ch['results'])} of {ch['need']} matches"
    if ch["kind"] == "clock":
        run = ch.get("run")
        return f"target {run['target']}s, {len(run['taps'])} tapped" if run else f"{ch['tries_left']} tries left"
    return f"{s['lat']:.5f}, {s['lon']:.5f}"
