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

app = Flask(__name__, static_folder=None)
lock = threading.Lock()
games = {}      # code -> state
versions = {}   # code -> int, bumped on every change


def db():
    con = sqlite3.connect(DB)
    con.execute("create table if not exists games (code text primary key, state text, updated real)")
    return con


def load():
    with db() as con:
        for code, state in con.execute("select code, state from games"):
            games[code] = json.loads(state)
            versions[code] = 1


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


@app.get("/gm")
def gm_page():
    return send_from_directory(HERE, "gm.html")


@app.get("/park")
def park_outline():
    return jsonify({"outline": game.PARK["outline"]})


# ---------------------------------------------------------------- game master

@app.post("/api/games")
def create():
    minutes = int((request.get_json(silent=True) or {}).get("minutes", 90))
    minutes = max(90, min(180, minutes))
    with lock:
        state = game.new_game(minutes)
        while state["code"] in games:
            state["code"] = game.new_game(minutes)["code"]
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
        game.reroll(state)
        save(state["code"])
        start_riddles(state["code"])
    return jsonify(ok=True)


@app.post("/api/games/<code>/start")
def start(code):
    with lock:
        state = get_game(code)
        need_gm(state)
        game.start(state)
        save(state["code"])
    return jsonify(ok=True)


@app.post("/api/games/<code>/end")
def end(code):
    with lock:
        state = get_game(code)
        need_gm(state)
        state["status"] = "finished"
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
        if state["status"] != "running" or team not in game.TEAMS:
            abort(409, "Nothing to approve")
        game.arrive(state, team, "gm")
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

@app.post("/api/join")
def join():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:24]
    team = body.get("team")
    if not name or team not in game.TEAMS:
        abort(400, "Name and team needed")
    with lock:
        state = get_game(body.get("code"))
        token = game.secrets.token_urlsafe(9)
        state["players"][token] = {"name": name, "team": team, "sim": bool(body.get("sim"))}
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


@app.post("/api/here")
def here():
    body = request.get_json(silent=True) or {}
    with lock:
        state = get_game(body.get("code"))
        p = state["players"].get(body.get("token"))
        if not p or state["status"] != "running":
            abort(409, "The game isn't running")
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
                yield "data: " + json.dumps(view()) + "\n\n"
            time.sleep(0.5)

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
def error(e):
    return jsonify(error=e.description), e.code


if __name__ == "__main__":
    load()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
