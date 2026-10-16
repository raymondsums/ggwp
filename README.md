# GGWP

Golden Gate Walk Party.


A live race through Golden Gate Park, for a group of friends on one day.
Players join from a link on their phones and pick red or blue. Each team
gets its own chain of stops. Finding a stop unlocks the next one, and the
first team to reach the shared final spot wins. A game master watches
everyone on a 3D map.

## Run it

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python server.py

- Game master: http://localhost:5057/gm. Pick a length, create the game, then
  copy the player link.
- Players: the link from the game master screen (`/?g=CODE`).
- Testing from a desk: "Red test player" and "Blue test player" on the game
  master screen open phone-sized windows where tapping the map moves you.

## How a game works

- **Routes.** Picked by the system from `data/park.json`. Both teams start at
  the same landmark and finish at the same one. In between, each team has its
  own stops, in the same order of types (landmark, hidden spot, landmark...).
  Many random pairs are tried and the fairest one is kept: total walking
  distance along real paths within 5%, and last legs within 30%. "Reroll"
  draws a new pair.
- **Length.** 90 minutes to 3 hours. 90 minutes is 4 stops plus the final, and
  each extra 30 minutes adds a stop. Distance is planned at 45 m per minute,
  which leaves time for thinking and wrong turns. Tune `METERS_PER_MINUTE` in
  `game.py` after a real round.
- **Clues.** Claude writes a clue for each landmark stop when the game is
  created, using the local CLI, the same way LifeOS does. If it fails, a plain
  description is used. Hidden spots get an area clue. More hints unlock over
  time, and every stop's hints end with a search circle on the map, then
  the exact spot marked. A stuck team always gets there; it just costs time.
- **Challenges.** Reaching a stop opens a challenge, and solving it unlocks
  the next clue. The order is Wordle (5 tries), Lock code (crack a
  code from right spot / wrong spot feedback), Switchboard (turn every light
  off, each phone owns some switches). Pace it out exists in the code but is
  out of the draw, since it leans on GPS the most. Longer games go round
  again at a harder level. A team sees a start screen first. The 10-minute
  clock starts when someone taps Start. Failing means a 5-minute wait, then
  a new puzzle, and after three fails an easier one. The game master sees
  the answers and can pass a stuck team. Rules live in `puzzles.py`.
- **Arriving.** Nothing counts by walking past. When a team thinks it is at
  its stop, someone taps **Check in** and their phone's position decides
  (within 30 m on open grass, 45 m elsewhere, accuracy better than 50 m). A
  wrong check-in rests the key for 1 minute for the whole team, and does not
  say how close they were. With a game master, "Location acting up? Ask the
  game master" sends an approval request.
- **Without a game master.** Open the site with no game code to create one
  from a phone. Anyone in the lobby can start it once clues are written and
  every team has a player. The game ends itself when time runs out; most
  stops wins, ties go to whoever reached their last stop first.
- **Landmark difficulty.** `data/landmark-difficulty.json` rates every
  landmark 1 (famous) to 5 (a plaque nobody knows), written once by
  `rate_landmarks.py` and safe to edit by hand. Teams' total difficulty stays
  within 1 point of each other.
- **Who sees what.** The game master sees everything. Teams see their own
  players and the other team's progress, not their position.

## Game day

The game has to be reachable by phones that are not on the tailnet.

    scripts/funnel.sh on     # prints the public https URL
    scripts/funnel.sh off    # closes it again

Only port 5057 goes public, on Funnel port 8443. LifeOS stays private.
Test the link from a phone on cellular before the day.

Phones only send location while the page is open and the screen is on. The
page keeps the screen awake, and positions that fail to send are retried.
Tell players to bring battery packs.

## Refreshing the map data

    scripts/fetch_osm.sh
    .venv/bin/python build_park.py

That rebuilds `data/park.json`: walkable paths, 119 landmarks, 222 hidden
spots, and walking distances between all of them. Paid or indoor places
(Botanical Garden, Tea Garden, museums) are never a stop inside, and routes
do not cut through them. A stop can name one of them, but the spot is always
its entrance, out in the open park. Stops sit at least 50 m inside the park's edge and never on
the street sidewalks that run along it.

To restart the server, stop whatever holds the port (`kill $(lsof -tiTCP:5057 -sTCP:LISTEN)`).
Matching on the command name misses it, since macOS shows the full Python path.

## Not built yet

Hide and seek mode, hidden spots (switched off, all stops are landmarks), photo proof, curse and power-up cards, and running on the
Mac mini as a LaunchAgent.
