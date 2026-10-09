# Up Next

A Home Assistant integration that plays the next episode of your shows on an
Apple TV, with one button press (or one Alexa routine).

- Keep a prioritised list of shows (plus a secondary list). "Play" walks the
  list and starts the first show that has a new episode out.
- Remembers where you are in every show, and follows along while you watch.
- Knows release dates (via TVmaze) and where each episode streams (via
  JustWatch), so weekly releases just work.
- Warns before a season or series finale.
- Shows with nothing coming up go "on a break": hidden until new episodes are
  announced, and then you get a notification.
- Auto-start: hold a show back and let Up Next start it at the right moment, so
  you never have to wait for the next episode once you've started.
- Remembers your preferred volume per show.
- A sidebar panel to manage it all: drag to reorder, add shows, set where you are.

Supported services on the Apple TV: **Apple TV+**, **Prime Video** and **Netflix**
(Netflix through the app's search, as it ignores deep links).

## Requirements

- Home Assistant 2026.8 or newer
- An Apple TV set up with the Apple TV integration (media player and remote)
- Optional: Alexa Media Player, for spoken announcements

## Installation

HACS → ⋮ → Custom repositories → add this repository as an *Integration*, install
**Up Next**, restart Home Assistant, then Settings → Devices & services → Add
integration → **Up Next**.

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
python -m pytest -q
python -m pyflakes custom_components tests
python tools/gen_strings.py   # regenerate strings.json / translations after text changes
```
