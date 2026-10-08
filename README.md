# GGWP

Golden Gate, Well Played.


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
- **Questions.** "Within 100 / 250 / 500 m?" and "Hotter or colder?" (mark a
  spot, walk at least 80 m, ask). The server knows where every stop is, so it
  answers instantly. One question per team every 3 minutes.
- **Arriving.** Two GPS fixes in a row inside the stop's radius (30 m on open
  grass, 45 m elsewhere), with accuracy better than 50 m. "We're here" asks
  the game master to approve, for when GPS is off.
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
(Botanical Garden, Tea Garden, museums) are left out, and routes do not cut
through them. Stops sit at least 50 m inside the park's edge and never on
the street sidewalks that run along it.

To restart the server, stop whatever holds the port (`kill $(lsof -tiTCP:5057 -sTCP:LISTEN)`).
Matching on the command name misses it, since macOS shows the full Python path.

## Not built yet

Hide and seek mode, photo proof, curse and power-up cards, and running on the
Mac mini as a LaunchAgent.
