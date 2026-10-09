"""How each streaming service behaves on an Apple TV.

"technical" is JustWatch's package name. "steps" is the recipe that gets from
"anything on screen" to "episode playing". All values were measured on a real
Apple TV (tvOS 26, Oct 2026).

Step types:
  ("url", None)          open the episode/show link (media_player.play_media url)
  ("wait", seconds)
  ("keys", [cmd, ...])   remote buttons, 0.4 s apart
  ("quick", [cmd, ...])  remote buttons, 0.15 s apart (in-app navigation)
  ("double", cmd)        two presses without delay (double-tap)
  ("text", None)         wait for a focused text field, then type the show title
  ("search", None)       cold-started Netflix: select (profile), up+left (Search
                         tab), down (keyboard), type once the field is focused
  ("quiet", None)        turn the volume down (app start-up chimes)
  ("loud", None)         back to the show's volume (or what it was)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Step = tuple[str, Any]

# Close the app in front: app switcher (double TV button), double swipe up, back out.
CLOSE_FRONT_APP: tuple[Step, ...] = (
    ("double", "home"),
    ("wait", 1.5),
    ("double", "up"),
    ("wait", 1.0),
    ("keys", ["menu"]),
    ("wait", 1.0),
)


@dataclass(frozen=True)
class Service:
    technical: str
    name: str
    app: str  # Apple TV app name (media_player source)
    bundle_ids: tuple[str, ...]
    steps: tuple[Step, ...]


NETFLIX = Service(
    technical="netflix",
    name="Netflix",
    app="Netflix",
    bundle_ids=("com.netflix.Netflix",),
    steps=(
        # Netflix ignores deep links on tvOS: it only opens. Make sure it's the
        # front app, close it, and cold-start it so we know where we are.
        ("quiet", None),  # no double Netflix chime
        ("url", None),
        ("wait", 4),
        *CLOSE_FRONT_APP,
        ("url", None),
        # Fixed order, never anything else: after the splash, "select" picks the
        # (only) profile; "up, left" goes to the Search tab; 1.5 s later "down"
        # activates the on-screen keyboard. Typing happens only when the Apple TV
        # then reports the field focused. If it doesn't, Netflix
        # was slow: close it and try the same once more with a longer wait.
        # (Never press select after up+left: on the profile screen that opens the
        # profile editor.)
        ("search", None),
        ("wait", 1.5),
        # (search already pressed "down" into the keyboard, onto "123")
        # "123" -> "abc" -> letters -> "a" -> right off the grid onto the first result
        ("quick", ["left", "down"] + ["left"] * 6 + ["right"] * 6),
        ("wait", 1),
        ("keys", ["select"]),  # first result -> show page
        ("wait", 3.5),
        ("loud", None),
        ("keys", ["select"]),  # "Play"/"Resume"/"Next episode"
    ),
)

SERVICES: dict[str, Service] = {
    s.technical: s
    for s in (
        NETFLIX,
        Service(
            technical="amazonprimevideo",
            name="Prime Video",
            app="Prime Video",
            bundle_ids=("com.amazon.aiv.AIVApp",),
            steps=(("url", None), ("wait", 10), ("keys", ["select"])),
        ),
        Service(
            technical="appletvplus",
            name="Apple TV+",
            app="TV",
            bundle_ids=("com.apple.TVWatchList",),
            steps=(("url", None), ("wait", 6), ("keys", ["select"])),
        ),
    )
}


def service_for(technical: str) -> Service | None:
    return SERVICES.get(technical)
