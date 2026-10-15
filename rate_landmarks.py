"""Rate how hard each landmark is to find from a clue, 1 to 5.

Run once after build_park.py, or again when the landmark list changes:

    .venv/bin/python rate_landmarks.py

Writes data/landmark-difficulty.json, {name: {"rating": n, "why": "..."}}.
It is a plain file on purpose, so the ratings can be read and corrected by
hand; the route picker reads it and keeps each team's total difficulty
close. Landmarks missing from the file count as 3.
"""

import json
import os
import subprocess

from server import find_claude

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "landmark-difficulty.json")

SYSTEM = (
    "You rate places in Golden Gate Park, San Francisco, for a treasure hunt. "
    "For each place, rate how hard it is for a group of friends who know SF a little "
    "to find it from a riddle and get to it on foot. 1: famous, big, on every map "
    "(de Young, Japanese Tea Garden, Stow Lake). 2: well known, easy to spot. "
    "3: known to regulars, needs some looking. 4: small or tucked away, a statue or "
    "garden most visitors miss. 5: a plaque, small artwork or marker almost nobody knows. "
    "Give a short reason in plain words, no em dashes."
)
SCHEMA = {
    "type": "object",
    "properties": {"ratings": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "rating": {"type": "integer"}, "why": {"type": "string"}},
        "required": ["name", "rating", "why"]}}},
    "required": ["ratings"],
}


def main():
    park = json.load(open(os.path.join(HERE, "data", "park.json")))
    places = sorted({(c["name"], c["kind"]) for c in park["candidates"] if c["type"] == "landmark"})
    prompt = "Rate each place:\n" + "\n".join(f"- {n} ({k})" for n, k in places)
    argv = [find_claude(), "-p", "--model", "sonnet", "--tools", "", "--no-session-persistence",
            "--output-format", "json", "--system-prompt", SYSTEM, "--json-schema", json.dumps(SCHEMA)]
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=600,
                          cwd=os.path.expanduser("~"))
    if proc.returncode != 0:
        raise SystemExit(f"claude CLI exited {proc.returncode}")
    out = json.loads(proc.stdout)
    data = out.get("structured_output") or json.loads(out.get("result") or "{}")
    names = {n for n, _ in places}
    ratings = {r["name"]: {"rating": max(1, min(5, int(r["rating"]))),
                           "why": r["why"].replace("—", ",").replace("–", ",")}
               for r in data.get("ratings", []) if r["name"] in names}
    with open(OUT + ".tmp", "w") as f:
        json.dump(dict(sorted(ratings.items())), f, indent=1, ensure_ascii=False)
    os.replace(OUT + ".tmp", OUT)
    missing = names - set(ratings)
    print(f"{len(ratings)} rated, {len(missing)} missing" + (f": {sorted(missing)}" if missing else ""))


if __name__ == "__main__":
    main()
