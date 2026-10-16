"""GGWP server: one Flask app for players and the game master.

    .venv/bin/python server.py        # http://localhost:5057

Games live in memory and are saved to SQLite on every change, so a restart
mid-game picks up where it left off. Phones get live updates over
server-sent events.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time

from flask import Flask, Response, abort, jsonify, request, send_from_directory

import game

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "games.db")
PORT = int(os.environ.get("PARKRACE_PORT", 5057))
# The mini binds to loopback only: Tailscale Funnel is the one way in.
HOST = os.environ.get("PARKRACE_HOST", "0.0.0.0")

app = Flask(__name__, static_folder=None)
lock = threading.Lock()
games = {}      # code -> state
created_by = {}  # ip -> recent game creation times, for a light rate limit
versions = {}   # code -> int, bumped on every change


def db():
    con = sqlite3.connect(DB)
    con.execute("create table if not exists games (code text primary key, state text, updated real)")
    return con


def load():
    with db() as con:
        for code, state in con.execute("select code, state from games"):
            # Wordle was called Parkle in games saved before 2026-10-15.
            games[code] = json.loads(state.replace('"parkle"', '"wordle"'))
            versions[code] = 1
    # A restart while clues were being written would leave the game stuck on
    # "Writing clues" for good, so those jobs start again.
    for code, state in games.items():
        if state.get("riddles_status") == "writing" and state["status"] == "lobby":
            start_riddles(code)
        elif state.get("riddles_status") == "writing":
            state["riddles_status"] = "failed"


def save(code):
    """Caller holds the lock."""
    versions[code] = versions.get(code, 0) + 1
    with db() as con:
        con.execute("insert or replace into games values (?, ?, ?)",
                    (code, json.dumps(games[code]), time.time()))


def get_game(code):
    state = games.get((code or "").upper())
    if not state:
        abort(404, "No game with that code")
    return state


def need_gm(state):
    if request.args.get("key") != state["gm_key"] and (request.get_json(silent=True) or {}).get("key") != state["gm_key"]:
        abort(403, "Game master link needed")


# ---------------------------------------------------------------- riddles

def find_claude():
    override = os.environ.get("CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    for loc in ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if os.path.exists(os.path.expanduser(loc)):
            return os.path.expanduser(loc)
    return "claude"


RIDDLE_SYSTEM = (
    "You write short clues for a treasure hunt in Golden Gate Park, San Francisco, "
    "played by a group of friends on foot. Each clue points at one real place without naming it. "
    "Rules: one or two sentences, under 25 words. Plain everyday words. No em dashes. "
    "Never use any word from the place's name. Lean on what a person would actually see there, "
    "or well known facts about it. Clear enough that a sharp local could get it, not so clear "
    "that anyone could."
)
RIDDLE_SCHEMA = {
    "type": "object",
    "properties": {"clues": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "clue": {"type": "string"}},
        "required": ["id", "clue"]}}},
    "required": ["clues"],
}


def write_riddles(code, plan_id):
    with lock:
        state = games[code]
        targets = game.riddle_targets(state)
        places = [{"id": str(i), "name": game.CANDS[i]["name"], "kind": game.CANDS[i]["kind"],
                   "area": game.section(game.pt(game.CANDS[i]))} for i in targets]
    prompt = "Write one clue for each place:\n" + json.dumps(places, indent=1)
    argv = [find_claude(), "-p", "--model", "sonnet", "--tools", "", "--no-session-persistence",
            "--output-format", "json", "--system-prompt", RIDDLE_SYSTEM,
            "--json-schema", json.dumps(RIDDLE_SCHEMA)]
    riddles, status = {}, "failed"
    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=180,
                              cwd=os.path.expanduser("~"))
        if proc.returncode == 0:
            out = json.loads(proc.stdout)
            data = out.get("structured_output") or json.loads(out.get("result") or "{}")
            for item in data.get("clues", []):
                text = item["clue"].replace("\u2014", ",").replace("\u2013", ",").strip()
                if item["id"] in {p["id"] for p in places}:
                    riddles[item["id"]] = text
            status = "ready" if riddles else "failed"
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        pass
    with lock:
        state = games.get(code)
        # A reroll while this was running makes these clues stale.
        if not state or id(state["plan"]) != plan_id:
            return
        state["riddles"] = riddles
        state["riddles_status"] = status
        save(code)


def start_riddles(code):
    plan_id = id(games[code]["plan"])
    threading.Thread(target=write_riddles, args=(code, plan_id), daemon=True).start()


# ---------------------------------------------------------------- pages

@app.get("/")
def player_page():
    return send_from_directory(HERE, "player.html")


@app.get("/sw.js")
def service_worker():
    return send_from_directory(HERE, "sw.js", mimetype="text/javascript", max_age=0)


@app.get("/gm")
def gm_page():
    return send_from_directory(HERE, "gm.html")


@app.get("/api/landmarks")
def landmarks():
    """Every landmark with its check-in circle, for the GPS test screen."""
    return jsonify([{"name": c["name"], "lat": c["lat"], "lon": c["lon"], "radius": c["radius"]}
                    for c in game.CANDS if c["type"] == "landmark"])


@app.get("/park")
def park_outline():
    return jsonify({"outline": game.PARK["outline"]})


# ---------------------------------------------------------------- game master

@app.post("/api/games")
def create():
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    minutes = max(90, min(180, int(game._num(body.get("minutes")) or 90)))
    teams = max(2, min(3, int(game._num(body.get("teams")) or 2)))
    mode = "self" if body.get("mode") == "self" else "gm"
    # Planning routes takes seconds, so it happens outside the lock; games in
    # play are not held up by someone creating a new one.
    # A header can be faked, so there is also a cap across everyone: planning
    # routes is heavy, and a flood of new games would slow games in play.
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    now = time.time()
    recent = [t for t in created_by.get(ip, []) if now - t < 600]
    everyone = [t for ts in created_by.values() for t in ts if now - t < 600]
    if len(recent) >= 6 or len(everyone) >= 20:
        abort(429, "Too many new games. Try again in a few minutes")
    created_by[ip] = recent + [now]
    state = game.new_game(minutes, teams=teams, mode=mode)
    with lock:
        while state["code"] in games:
            state["code"] = game.secrets.token_hex(3).upper()
        games[state["code"]] = state
        save(state["code"])
        start_riddles(state["code"])
    return jsonify({"code": state["code"], "key": state["gm_key"]})


@app.post("/api/games/<code>/reroll")
def reroll(code):
    with lock:
        state = get_game(code)
        need_gm(state)
        if state["status"] != "lobby":
            abort(409, "Routes are locked once the game starts")
        minutes, teams = state["minutes"], len(state["teams"])
    plan = game.plan_routes(minutes, None, teams)
    with lock:
        state = get_game(code)
        if state["status"] != "lobby":
            abort(409, "Routes are locked once the game starts")
        game.reroll(state, plan=plan)
        save(state["code"])
        start_riddles(state["code"])
    return jsonify(ok=True)


@app.post("/api/games/<code>/teams")
def team_count(code):
    """Change 2 or 3 teams from the lobby. The game master can, and so can
    any player in a game with no game master."""
    body = request.get_json(silent=True) or {}
    n = max(2, min(3, int(game._num(body.get("teams")) or 2)))
    with lock:
        state = get_game(code)
        lobby_actor(state, body)
        if state["status"] != "lobby":
            abort(409, "Teams are set once the game starts")
        if len(state["teams"]) == n:
            return jsonify(ok=True)
        minutes = state["minutes"]
    plan = game.plan_routes(minutes, None, n)
    with lock:
        state = get_game(code)
        if state["status"] != "lobby":
            abort(409, "Teams are set once the game starts")
        game.set_team_count(state, n, plan)
        save(state["code"])
        start_riddles(state["code"])
    return jsonify(ok=True)


def lobby_actor(state, body):
    """Who may change teams in the lobby: the game master, or in a game with
    no game master, any player. Returns a name for the activity log."""
    if state.get("mode") == "self":
        if body.get("token") not in state["players"]:
            abort(403, "Not in this game")
        return state["players"][body["token"]]["name"]
    need_gm(state)
    return "The game master"


@app.post("/api/games/<code>/shuffle")
def shuffle(code):
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(code)
        who = lobby_actor(state, body)
        if state["status"] != "lobby":
            abort(409, "Teams are set once the game starts")
        game.shuffle_teams(state, who)
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/leave")
def leave():
    """Exit from the lobby. Once the game starts, players stay on the books."""
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(body.get("code"))
        p = state["players"].get(body.get("token"))
        if not p:
            return jsonify(ok=True)
        if state["status"] != "lobby":
            abort(409, "The game has started")
        del state["players"][body["token"]]
        game.log(state, f"{p['name']} left", p["team"])
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/switch")
def switch():
    """A player moves to another team, any time before the start."""
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(body.get("code"))
        if body.get("token") not in state["players"]:
            abort(403, "Not in this game")
        if state["status"] != "lobby":
            abort(409, "Teams are set once the game starts")
        if body.get("team") not in state["teams"]:
            abort(400, "No such team")
        game.switch_team(state, body["token"], body["team"])
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/games/<code>/start")
def start(code):
    with lock:
        state = get_game(code)
        need_gm(state)
        if state["status"] != "lobby":
            abort(409, "The game has already started")
        # Starting mid-write would swap clue 1 under the players' feet.
        if state["riddles_status"] == "writing":
            abort(409, "Still setting up the game")
        empty = [t for t in state["teams"] if not any(p["team"] == t for p in state["players"].values())]
        if empty:
            abort(409, f"{empty[0].capitalize()} has nobody yet")
        game.start(state)
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/games/<code>/end")
def end(code):
    with lock:
        state = get_game(code)
        need_gm(state)
        # Closed from the lobby, nobody played, so the final spot stays a secret.
        if state["status"] == "lobby":
            state["closed"] = True
        state["status"] = "finished"
        state["finished_at"] = time.time()
        state["pending"] = {}
        game.log(state, "Game ended by the game master")
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/games/<code>/approve")
def approve(code):
    team = (request.get_json(silent=True) or {}).get("team")
    with lock:
        state = get_game(code)
        need_gm(state)
        if state["status"] != "running" or team not in state["teams"] or team not in state["pending"]:
            abort(409, "Nothing to approve")
        game.arrive(state, team, "gm")
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/games/<code>/message")
def message(code):
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(code)
        need_gm(state)
        err = game.send_message(state, body.get("to") or {}, body.get("text"))
        if err:
            abort(400, err)
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/games/<code>/deny")
def deny(code):
    team = (request.get_json(silent=True) or {}).get("team")
    with lock:
        state = get_game(code)
        need_gm(state)
        state["pending"].pop(team, None)
        game.log(state, f"{str(team).capitalize()} check-in turned down", team)
        save(state["code"])
    return jsonify(ok=True)


# ---------------------------------------------------------------- players

@app.get("/api/info")
def info():
    """What the join screen needs before anyone has joined: which teams."""
    with lock:
        state = get_game(request.args.get("g"))
        return jsonify(teams=list(state["teams"]), status=state["status"], mode=state.get("mode", "gm"))


@app.post("/api/join")
def join():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:24]
    team = body.get("team")
    with lock:
        state = get_game(body.get("code"))
        if not name or team not in state["teams"]:
            abort(400, "Name and team needed")
        if state["status"] == "finished":
            abort(409, "This game is over")
        # Two players with one name would muddle Mind Meld and the log.
        taken = {p["name"].lower() for p in state["players"].values()}
        base, n = name, 2
        while name.lower() in taken:
            name = f"{base[:21]} {n}"
            n += 1
        token = game.secrets.token_urlsafe(9)
        state["players"][token] = {"name": name, "team": team, "sim": bool(body.get("sim")),
                                   "pid": game.secrets.token_hex(4)}
        game.log(state, f"{name} joined {team.capitalize()}", team)
        save(state["code"])
    return jsonify({"token": token})


@app.post("/api/pos")
def pos():
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(body.get("code"))
        if body.get("token") not in state["players"]:
            abort(403, "Not in this game")
        for fix in (body.get("fixes") or [])[-30:]:
            game.record_fix(state, body["token"], fix)
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/ask")
def ask():
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(body.get("code"))
        ok, msg = game.ask(state, body.get("token"), body.get("kind"), body.get("size"))
        if ok:
            save(state["code"])
    return jsonify(ok=ok, message=msg)


@app.post("/api/challenge")
def challenge():
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(body.get("code"))
        ok, msg = game.challenge_action(state, body.get("token"), body.get("action"), body.get("value"))
        if ok:
            save(state["code"])
    return jsonify(ok=ok, message=msg)


@app.post("/api/games/<code>/pass")
def pass_challenge(code):
    team = (request.get_json(silent=True) or {}).get("team")
    with lock:
        state = get_game(code)
        need_gm(state)
        if team not in state["teams"]:
            abort(400, "Pick a team")
        if state["status"] != "running":
            abort(409, "The game isn't running")
        game.pass_challenge(state, team)
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/checkin")
def checkin():
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(body.get("code"))
        ok, msg = game.check_in(state, body.get("token"), body.get("reading"))
        save(state["code"])
    return jsonify(ok=ok, message=msg)


@app.post("/api/self-start")
def self_start():
    """No game master: any player in the game can start it from the lobby."""
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(body.get("code"))
        if state.get("mode") != "self":
            abort(403, "The game master starts this game")
        if body.get("token") not in state["players"]:
            abort(403, "Not in this game")
        if state["status"] != "lobby":
            return jsonify(ok=True)
        if state["riddles_status"] == "writing":
            abort(409, "Still setting up the game")
        empty = [t for t in state["teams"] if not any(p["team"] == t for p in state["players"].values())]
        if empty:
            abort(409, f"{empty[0].capitalize()} has nobody yet")
        game.start(state)
        game.log(state, f"{state['players'][body['token']]['name']} started the game")
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/here")
def here():
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(body.get("code"))
        p = state["players"].get(body.get("token"))
        if not p or state["status"] != "running":
            abort(409, "The game isn't running")
        if state.get("mode") == "self":
            abort(409, "No game master to check. Your location has to do it")
        if state["teams"][p["team"]].get("challenge"):
            abort(409, "You're already checked in here")
        state["pending"][p["team"]] = time.time()
        game.log(state, f"{p['name']} says {p['team'].capitalize()} is at the spot", p["team"])
        save(state["code"])
    return jsonify(ok=True)


# ---------------------------------------------------------------- live updates

@app.get("/api/stream")
def stream():
    code = (request.args.get("g") or "").upper()
    token = request.args.get("p")
    key = request.args.get("key")
    with lock:
        state = get_game(code)
        if key:
            need_gm(state)
        elif token not in state["players"]:
            abort(403, "Not in this game")

    def view():
        with lock:
            s = games[code]
            return game.gm_view(s) if key else game.player_view(s, token)

    def events():
        last, beat = None, 0
        while True:
            v = versions.get(code)
            # Re-sent every 15 seconds even unchanged, so hint timers and
            # "last seen" stamps move without a change to trigger them.
            if v != last or time.time() - beat > 15:
                last, beat = v, time.time()
                # A bug in one view must not close the stream and leave a
                # phone frozen; it is logged and the next update tries again.
                try:
                    v = view()
                    # The player left (or was removed) from another tab.
                    if v is None:
                        yield "event: gone\ndata: {}\n\n"
                        return
                    yield "data: " + json.dumps(v) + "\n\n"
                except Exception:
                    app.logger.exception("view failed for game %s", code)
            # The game master's view is big, so it updates at most once a
            # second; players' views are small and update twice a second.
            time.sleep(1.0 if key else 0.5)

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.errorhandler(TypeError)
@app.errorhandler(ValueError)
@app.errorhandler(KeyError)
@app.errorhandler(AttributeError)
def bad_input(e):
    """Odd input (a list where a name goes, text where a number goes) gets a
    plain 400 rather than a server error page."""
    app.logger.warning("bad input on %s: %r", request.path, e)
    return jsonify(error="That request didn't make sense"), 400


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(429)
def error(e):
    return jsonify(error=e.description), e.code


def ticker():
    """Runs out challenge clocks once a second, even when nobody is tapping."""
    while True:
        time.sleep(1)
        with lock:
            for code, state in list(games.items()):
                # One broken game must never stop the clock for the rest.
                try:
                    if game.tick(state):
                        save(code)
                except Exception:
                    app.logger.exception("tick failed for game %s", code)


if __name__ == "__main__":
    load()
    threading.Thread(target=ticker, daemon=True).start()
    app.run(host=HOST, port=PORT, threaded=True)
