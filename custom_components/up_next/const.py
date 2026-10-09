"""Constants for Up Next."""

from typing import Final

DOMAIN: Final = "up_next"
NAME: Final = "Up Next"

# Main entry
CONF_MEDIA_PLAYER: Final = "media_player"
CONF_REMOTE: Final = "remote"
CONF_COUNTRY: Final = "country"
CONF_LANGUAGE: Final = "language"
CONF_ANNOUNCE: Final = "announce_entity"
CONF_SUBSCRIPTIONS: Final = "subscriptions"

DEFAULT_COUNTRY: Final = "NL"
DEFAULT_LANGUAGE: Final = "en"

# Show subentries
SUBENTRY_SHOW: Final = "show"
CONF_TITLE: Final = "title"
CONF_JW_ID: Final = "jw_id"
CONF_IMDB_ID: Final = "imdb_id"
CONF_SERVICE: Final = "service"
CONF_SEASON: Final = "season"
CONF_EPISODE: Final = "episode"
CONF_QUERY: Final = "query"
CONF_RESULT: Final = "result"
CONF_START: Final = "start"
CONF_LAST_WATCHED: Final = "last_watched"
CONF_NEXT_EPISODE: Final = "next_episode"

START_BEGINNING: Final = "beginning"
START_CAUGHT_UP: Final = "caught_up"
START_SEASON: Final = "season_start"
START_LATEST: Final = "latest"
START_CUSTOM: Final = "custom"

SIGNAL_UPDATED: Final = f"{DOMAIN}_updated"

REFRESH_HOURS: Final = 6
# Fraction of an episode that counts as "watched".
WATCHED_FRACTION: Final = 0.85
