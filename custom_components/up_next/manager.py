"""Up Next core: a prioritised queue of shows; play the first one with a new episode."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import re
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import Catalog, CatalogError, Episode, ShowData
from .const import (
    CONF_ANNOUNCE,
    CONF_COUNTRY,
    CONF_EPISODE,
    CONF_JW_ID,
    CONF_LANGUAGE,
    CONF_LAST_WATCHED,
    CONF_MEDIA_PLAYER,
    CONF_REMOTE,
    CONF_SEASON,
    CONF_SERVICE,
    CONF_SUBSCRIPTIONS,
    CONF_TITLE,
    DEFAULT_COUNTRY,
    DEFAULT_LANGUAGE,
    DOMAIN,
    REFRESH_HOURS,
    SIGNAL_UPDATED,
    SUBENTRY_SHOW,
    WATCHED_FRACTION,
)
from .schedule import NEVER, Lane, Plan, latest_start
from .services_catalog import CLOSE_FRONT_APP, SERVICES, Service, service_for

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
# How long after we started a show we assume unidentified playback in that app is it.
SESSION_WINDOW = timedelta(hours=4)
# A show with nothing out and nothing due within this many days is "on a break".
BREAK_DAYS = 14
# Learn the volume once you're this far into an episode (you've settled on it by then).
VOLUME_FRACTION = 0.2
# Apps that don't report a position: assume you've settled after this long.
VOLUME_FALLBACK = timedelta(minutes=10)
VOLUME_WAIT = 5  # seconds to wait for the Apple TV to report its volume before "quiet"
VOLUME_AUTO = "auto"  # learn it and restore it (default)
VOLUME_FIXED = "fixed"  # always set this volume
VOLUME_OFF = "off"  # leave the volume alone
VOLUME_MODES = (VOLUME_AUTO, VOLUME_FIXED, VOLUME_OFF)
# Apps that report nothing (Netflix): count playing time against the episode's length.
DEFAULT_RUNTIME = 45  # minutes, when the catalog doesn't know
STEP_TIMEOUT = 25  # seconds; a stuck Apple TV call must not block the play button forever
QUICK_KEY_DELAY = 0.15  # seconds between presses when moving around inside an app
SEARCH_TIMEOUT = 75  # seconds for the whole search step, including one restart
SEARCH_BOOT_WAITS = (8, 16)  # splash time before "select": normal, then a slow day

STATUS_AVAILABLE = "available"
STATUS_UPCOMING = "upcoming"  # announced, not out yet
STATUS_WAITING = "waiting"  # no next episode announced yet (show still running)
STATUS_FINISHED = "finished"  # watched everything, show ended
STATUS_UNAVAILABLE = "unavailable"  # out, but not on any of your services
STATUS_UNKNOWN = "unknown"  # catalog not loaded

LIST_MAIN = "main"
LIST_SECONDARY = "secondary"
LISTS = (LIST_MAIN, LIST_SECONDARY)
HOLD_MANUAL = "manual"
HOLD_AUTO = "auto"

_EP_RE = re.compile(r"season\s*(\d+)\D+?(?:ep(?:isode)?\.?)\s*(\d+)", re.I)
_SXE_RE = re.compile(r"\bS(\d{1,2})\s*[:.]?\s*E(\d{1,3})\b", re.I)


@dataclass
class Next:
    """What 'play' would do right now for one show."""

    status: str
    episode: Episode | None
    url: str | None = None
    service: Service | None = None
    finale: str | None = None  # "season" / "series"


class ShowTracker:
    """Runtime state for one show subentry."""

    def __init__(self, manager: UpNextManager, subentry_id: str, data: dict[str, Any]) -> None:
        self.manager = manager
        self.subentry_id = subentry_id
        self.title: str = data[CONF_TITLE]
        self.jw_id: str = data[CONF_JW_ID]
        self.data: ShowData | None = None
        self.blind = False  # last sighting had no position info (count time instead)
        self.seen: tuple[int, int] | None = None
        self.refreshed: datetime | None = None
        self.error: str | None = None
        progress = manager.progress.setdefault(subentry_id, {})
        if "service" not in progress and data.get(CONF_SERVICE):
            progress["service"] = data[CONF_SERVICE]
        if "last_watched" not in progress and "start" not in progress:
            if data.get(CONF_LAST_WATCHED):
                progress["last_watched"] = list(data[CONF_LAST_WATCHED])
            elif data.get(CONF_SEASON):  # v0.1: "next is SxEy"
                progress["start"] = [int(data[CONF_SEASON]), int(data.get(CONF_EPISODE) or 1)]

    # ---- progress -------------------------------------------------------
    @property
    def _p(self) -> dict[str, Any]:
        return self.manager.progress.setdefault(self.subentry_id, {})

    @property
    def last_watched(self) -> tuple[int, int] | None:
        v = self._p.get("last_watched")
        return (v[0], v[1]) if v else None

    @property
    def in_progress(self) -> tuple[int, int] | None:
        v = self._p.get("in_progress")
        return (v[0], v[1]) if v else None

    @property
    def started_at(self) -> datetime | None:
        v = self._p.get("started_at")
        return dt_util.parse_datetime(v) if v else None

    @property
    def service_key(self) -> str | None:
        """The service this show was last played on (sticky, for app-side resume)."""
        return self._p.get("service")

    @property
    def list_name(self) -> str:
        return self._p.get("list", LIST_MAIN)

    @property
    def hold(self) -> str | None:
        """None (play normally), 'manual' (skip) or 'auto' (start on a planned day)."""
        return self._p.get("hold")

    @property
    def volume_mode(self) -> str:
        return self._p.get("volume_mode", VOLUME_AUTO)

    @property
    def volume(self) -> float | None:
        """Learned (auto) or chosen (fixed) volume, 0..1."""
        return self._p.get("volume")

    def on_break(self, now: datetime | None = None) -> bool:
        """Nothing to watch and nothing due soon: hidden from the list and announcements."""
        if self.data is None:
            return False
        now = now or dt_util.utcnow()
        nxt = self.next(now)
        if nxt.status in (STATUS_WAITING, STATUS_FINISHED):
            return True
        if nxt.status == STATUS_UPCOMING and nxt.episode and nxt.episode.airstamp:
            return nxt.episode.airstamp - now > timedelta(days=BREAK_DAYS)
        return False

    def episode_days(self, today: date, until_finale: bool = False) -> tuple[list[date], int | None]:
        """Release day of each unwatched episode from the next one on.

        Returns (days, index of the current season's finale or None).
        """
        nxt = self.next().episode
        if self.data is None or nxt is None:
            return [], None
        days: list[date] = []
        finale_idx = None
        for ep in self.data.episodes:
            if ep.key < nxt.key:
                continue
            if ep.links and (ep.airstamp is None or dt_util.as_local(ep.airstamp).date() > today):
                day = today  # out already, whatever the schedule said
            elif ep.airstamp:
                day = dt_util.as_local(ep.airstamp).date()
            else:
                break  # no date known: stop here
            days.append(day)
            if ep.season == nxt.season and self.data.finale(ep):
                finale_idx = len(days) - 1
                if until_finale:
                    break
            if ep.season != nxt.season and until_finale:
                break
        return days, finale_idx

    def _set(self, **values: Any) -> None:
        for key, value in values.items():
            if value is None:
                self._p.pop(key, None)
            elif isinstance(value, tuple):
                self._p[key] = list(value)
            elif isinstance(value, datetime):
                self._p[key] = value.isoformat()
            else:
                self._p[key] = value
        self.manager.save()

    def mark_watched(self, key: tuple[int, int]) -> None:
        current = self.last_watched
        if current is None or key > current:
            self._set(last_watched=key)
        if self.in_progress is not None and self.in_progress <= key:
            self._set(in_progress=None)
        self._p.pop("start", None)
        self.manager.save()

    def set_last_watched(self, key: tuple[int, int] | None) -> None:
        self._p.pop("start", None)
        self._p["last_watched"] = list(key) if key else None  # explicit None: "nothing yet"
        self._set(in_progress=None, started_at=None)

    def set_next(self, season: int, episode: int) -> None:
        """Make SxEy the next episode to play."""
        if self.data is None:
            self._set(in_progress=None)
            self._p["start"] = [season, episode]
            self.manager.save()
            return
        prev = self.data.before((season, episode))
        self.set_last_watched(prev.key if prev else None)

    def step_back(self) -> None:
        nxt = self.next().episode
        key = nxt.key if nxt else (self.last_watched or (1, 1))
        prev = self.data.before(key) if self.data else None
        if prev:
            self.set_next(*prev.key)

    # ---- catalog --------------------------------------------------------
    async def refresh(self, force: bool = False) -> None:
        if (
            not force
            and self.refreshed
            and dt_util.utcnow() - self.refreshed < timedelta(hours=REFRESH_HOURS)
        ):
            return
        try:
            self.data = await self.manager.catalog.show(self.jw_id)
            self.refreshed = dt_util.utcnow()
            self.error = None
            start = self._p.pop("start", None)
            if start and self.data:
                prev = self.data.before((start[0], start[1]))
                self._set(last_watched=prev.key if prev else None)
        except CatalogError as err:
            self.error = str(err)
            _LOGGER.warning("Could not refresh %s: %s", self.title, err)
        self.manager.notify()

    # ---- decide ---------------------------------------------------------
    def _pick(self, links: dict[str, str]) -> tuple[Service, str] | None:
        """Best subscribed service for these links (sticky service first)."""
        subs = self.manager.subscriptions
        order = ([self.service_key] if self.service_key else []) + subs
        for key in order:
            if key in subs and key in links and (svc := service_for(key)):
                return svc, links[key]
        return None

    def next(self, now: datetime | None = None) -> Next:
        if self.data is None:
            return Next(STATUS_UNKNOWN, None)
        now = now or dt_util.utcnow()
        target: Episode | None = None
        if self.in_progress:
            target = self.data.find(*self.in_progress)
        if target is None:
            target = self.data.after(self.last_watched)
        if target is None:
            return Next(STATUS_FINISHED if self.data.ended else STATUS_WAITING, None)
        finale = self.data.finale(target)
        if picked := self._pick(target.links):
            return Next(STATUS_AVAILABLE, target, picked[1], picked[0], finale)
        if target.airstamp and target.airstamp > now:
            return Next(STATUS_UPCOMING, target, finale=finale)
        if target.airstamp and now - target.airstamp < timedelta(days=2):
            # Aired, but the catalog hasn't caught up: the show page will have it.
            if picked := self._pick(self.data.show_links):
                return Next(STATUS_AVAILABLE, target, picked[1], picked[0], finale)
        return Next(STATUS_UNAVAILABLE, target, finale=finale)

    def status_line(self) -> str:
        """Short human description for lists."""
        base = self._status_line()
        hold = self.manager.hold_text(self)
        return f"{hold} · {base}" if hold else base

    def _status_line(self) -> str:
        nxt = self.next()
        ep = nxt.episode
        if nxt.status == STATUS_UNKNOWN:
            return "Loading…" if not self.error else "Catalog unavailable"
        if nxt.status == STATUS_WAITING:
            return "All caught up · no new episode announced"
        if nxt.status == STATUS_FINISHED:
            return "Finished"
        assert ep is not None
        parts = [ep.code + (f" · {ep.title}" if _real_title(ep.title) else "")]
        if nxt.finale:
            parts.append(f"{nxt.finale} finale")
        if nxt.status == STATUS_AVAILABLE and nxt.service:
            parts.append(f"ready on {nxt.service.name}")
        elif nxt.status == STATUS_UPCOMING and ep.airstamp:
            parts.append(f"out {_when(ep.airstamp)}")
        elif nxt.status == STATUS_UNAVAILABLE:
            parts.append("not on your services")
        return " · ".join(parts)

    def short_status(self) -> str:
        """Compact status for the integration page."""
        base = self._short_status()
        hold = self.manager.hold_text(self)
        return f"{hold} · {base}" if hold else base

    def _short_status(self) -> str:
        nxt = self.next()
        ep = nxt.episode
        last = self.data.find(*self.last_watched) if self.data and self.last_watched else None
        last_finale = self.data.finale(last) if self.data and last else None
        if nxt.status == STATUS_FINISHED:
            return "finished (series finale watched)"
        if nxt.status == STATUS_WAITING:
            if last_finale:
                return f"caught up after the {last_finale} finale · nothing announced yet"
            return "caught up · nothing announced yet"
        if nxt.status == STATUS_UNKNOWN or ep is None:
            return "loading…"
        text = f"next {ep.code}"
        if nxt.finale:
            text += f" ({nxt.finale} finale)"
        if nxt.status == STATUS_AVAILABLE:
            return text + " · out now"
        if nxt.status == STATUS_UPCOMING and ep.airstamp:
            prefix = f"caught up after the {last_finale} finale · " if last_finale else ""
            return prefix + text + f" · out {_date(ep.airstamp)}"
        return text + " · not on your services"

    # ---- tracking -------------------------------------------------------
    def runtime(self, key: tuple[int, int]) -> int:
        """Episode length in minutes (catalog, else the show's typical length)."""
        if self.data:
            if (ep := self.data.find(*key)) and ep.runtime:
                return ep.runtime
            known = sorted(e.runtime for e in self.data.episodes if e.runtime)
            if known:
                return known[len(known) // 2]
        return DEFAULT_RUNTIME

    def add_playtime(self, seconds: float) -> bool:
        """Count playing time for the episode in progress; mark it watched at 90%.

        Returns True if an episode was marked watched.
        """
        key = self.in_progress
        if key is None or seconds <= 0:
            return False
        if self._p.get("played_ep") != list(key):
            self._p["played_ep"] = list(key)
            self._p["played_s"] = 0.0
        self._p["played_s"] = self._p.get("played_s", 0.0) + seconds
        need = self.runtime(key) * 60
        if self._p["played_s"] < need * WATCHED_FRACTION:
            self.manager.save()
            return False
        extra = max(0.0, self._p["played_s"] - need)
        self.mark_watched(key)
        # still playing: Netflix autoplays the next one
        nxt = self.data.after(key) if self.data else None
        if nxt is not None:
            self._set(in_progress=nxt.key, started_at=dt_util.utcnow())
            self._p["played_ep"] = list(nxt.key)
            self._p["played_s"] = extra
        self.manager.save()
        return True

    def identify(self, attrs: dict[str, Any]) -> tuple[int, int] | None:
        """Work out which episode the Apple TV is playing, if it's this show."""
        app = attrs.get("app_id")
        svc_key = next((k for k, s in SERVICES.items() if app in s.bundle_ids), None)
        if svc_key is None:
            return None
        cid = str(attrs.get("media_content_id") or "")
        if cid and self.data:
            for ep in self.data.episodes:
                if any(cid in link for link in ep.links.values()):
                    return ep.key
        title = str(attrs.get("media_title") or "")
        series = str(attrs.get("media_series_title") or "")
        if not _same_show(self.title, title) and not _same_show(self.title, series):
            if self.data and title:  # some apps report the episode title as the title
                for ep in self.data.episodes:
                    if ep.title and _norm(ep.title) == _norm(title):
                        return ep.key
            if svc_key != self.service_key:
                return None
            return (0, 0) if not title else None
        if attrs.get("media_season") and attrs.get("media_episode"):
            try:
                return (int(attrs["media_season"]), int(attrs["media_episode"]))
            except ValueError:
                pass
        for text in (attrs.get("media_artist"), attrs.get("media_album_name"), title):
            if text and (m := _EP_RE.search(str(text)) or _SXE_RE.search(str(text))):
                return (int(m.group(1)), int(m.group(2)))
        return (0, 0)  # it's this show, episode unknown

    @callback
    def observe(self, attrs: dict[str, Any], playing: bool) -> bool:
        """Update progress from now-playing info. Returns True if it changed."""
        key = self.identify(attrs)
        if key is None:
            return False
        if key == (0, 0):
            if self.manager.last_started != self.subentry_id:
                return False
            started = self.started_at
            if not started or dt_util.utcnow() - started > SESSION_WINDOW:
                return False
            # Our show, in our session, but the app doesn't say which episode.
            nxt = self.next().episode
            key = self.in_progress or (nxt.key if nxt else None)
            if key is None:
                return False
        self.blind = not (attrs.get("media_position") and attrs.get("media_duration"))
        self.seen = key
        changed = False
        if key != self.in_progress and playing:
            if self.in_progress and key > self.in_progress:
                self.mark_watched(self.in_progress)  # autoplay moved on
            if self.last_watched is None or key > self.last_watched:
                self._set(in_progress=key, started_at=dt_util.utcnow())
                changed = True
        pos, dur = attrs.get("media_position"), attrs.get("media_duration")
        if playing and self.volume_mode == VOLUME_AUTO:
            self._learn_volume(key, attrs, pos, dur)
        if pos and dur and dur > 0 and pos / dur >= WATCHED_FRACTION:
            if self.last_watched != key:
                self.mark_watched(key)
                changed = True
        return changed

    def _learn_volume(self, key: tuple[int, int], attrs: dict[str, Any], pos: Any, dur: Any) -> None:
        """Remember the volume once per episode, 20% in."""
        level = attrs.get("volume_level")
        if level is None or attrs.get("is_volume_muted") or list(key) == self._p.get("volume_ep"):
            return
        if pos and dur and dur > 0:
            settled = pos / dur >= VOLUME_FRACTION
        else:
            started = self.started_at
            settled = bool(started and dt_util.utcnow() - started >= VOLUME_FALLBACK)
        if settled and pos != 0:
            self._p["volume"] = round(float(level), 3)
            self._p["volume_ep"] = list(key)
            _LOGGER.info("Learned volume %s for %s", self._p["volume"], self.title)
            self.manager.save()
            self.manager.notify()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _same_show(show: str, reported: str) -> bool:
    if not reported:
        return False
    a, b = _norm(show), _norm(reported)
    return bool(a) and (a == b or b.startswith(a) or a.startswith(b))


class UpNextManager:
    """One per config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        opts = {**entry.data, **entry.options}
        self.media_player: str = opts[CONF_MEDIA_PLAYER]
        self.remote: str | None = opts.get(CONF_REMOTE)
        self.announce_entity: str | None = opts.get(CONF_ANNOUNCE)
        self.subscriptions: list[str] = [
            s for s in opts.get(CONF_SUBSCRIPTIONS, list(SERVICES)) if s in SERVICES
        ]
        self.catalog = Catalog(
            async_get_clientsession(hass),
            opts.get(CONF_COUNTRY, DEFAULT_COUNTRY),
            opts.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.progress: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.shows: dict[str, ShowTracker] = {}
        self._unsub: list = []
        self._play_lock = asyncio.Lock()
        self.last_started: str | None = None
        self.signature: str = ""
        self.plans: dict[str, Plan] = {}
        self._clock: tuple[str, datetime] | None = None  # (show, playing since) for blind apps
        self._quiet_from: float | None = None  # volume before a "quiet" launch phase

    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self.progress = stored.get("progress", {})
        self.last_started = stored.get("last_started")
        for sub in self.entry.subentries.values():
            if sub.subentry_type == SUBENTRY_SHOW:
                self.shows[sub.subentry_id] = ShowTracker(self, sub.subentry_id, dict(sub.data))
        for sid in list(self.progress):  # forget removed shows
            if sid not in self.shows:
                self.progress.pop(sid)
        order = [s for s in stored.get("order", []) if s in self.shows]
        if not order and stored.get("favorite") in self.shows:  # v0.1
            order = [stored["favorite"]]
        self.order = order + [s for s in self.shows if s not in order]
        self._cleanup_v01_devices()
        self._unsub.append(
            async_track_state_change_event(self.hass, [self.media_player], self._player_changed)
        )
        self._unsub.append(
            async_track_time_interval(self.hass, self._tick, timedelta(minutes=1))
        )
        # new day: auto-starts and "out today" change
        self._unsub.append(
            async_track_time_change(self.hass, self._new_day, hour=0, minute=1, second=0)
        )
        await asyncio.gather(*(s.refresh() for s in self.shows.values()))
        self.notify()

    def _cleanup_v01_devices(self) -> None:
        """v0.1 made a device per show; shows are plain queue items now."""
        dev_reg = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(dev_reg, self.entry.entry_id):
            if (DOMAIN, self.entry.entry_id) not in device.identifiers:
                dev_reg.async_remove_device(device.id)
        ent_reg = er.async_get(self.hass)
        for old in ("favorite", "favorite_next", "queue"):
            uid = f"{self.entry.entry_id}_{old}"
            for domain in ("select", "sensor", "todo"):
                if entity_id := ent_reg.async_get_entity_id(domain, DOMAIN, uid):
                    ent_reg.async_remove(entity_id)

    async def async_unload(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {"progress": self.progress, "order": self.order, "last_started": self.last_started}

    async def async_save_now(self) -> None:
        await self._store.async_save(self._data())

    @callback
    def save(self) -> None:
        self._store.async_delay_save(self._data, 2)

    @callback
    def _new_day(self, _now: datetime) -> None:
        self.notify()

    @callback
    def notify(self) -> None:
        self.plan()
        self._check_breaks()
        self._update_titles()
        async_dispatcher_send(self.hass, f"{SIGNAL_UPDATED}_{self.entry.entry_id}")

    @callback
    def _update_titles(self) -> None:
        """Show each show's status in its entry title on the integration page."""
        for sid, show in self.shows.items():
            sub = self.entry.subentries.get(sid)
            if sub is None or show.data is None:
                continue
            title = f"{show.title} — {show.short_status()}"
            if sub.title != title:
                self.hass.config_entries.async_update_subentry(self.entry, sub, title=title)

    @callback
    def _check_breaks(self) -> None:
        """Track shows going on / coming back from a break; tell you when one is back."""
        for sid, show in self.shows.items():
            if show.data is None:
                continue
            now_break = show.on_break()
            was = show._p.get("on_break")
            if was == now_break:
                continue
            show._p["on_break"] = now_break
            self.save()
            if was and not now_break:  # first sighting just records the state
                text = _comeback_text(show)
                show._p["comeback"] = text
                persistent_notification.async_create(
                    self.hass, text, title="Up Next", notification_id=f"{DOMAIN}_back_{sid}"
                )
            elif now_break:
                show._p.pop("comeback", None)

    def _take_comebacks(self) -> list[str]:
        """'X is back' lines, said once with the next announcement."""
        out = []
        for show in self.queue:
            if text := show._p.pop("comeback", None):
                out.append(text)
        if out:
            self.save()
        return out

    def set_volume(self, show: ShowTracker, mode: str, volume: float | None = None) -> None:
        show._p["volume_mode"] = mode
        if volume is not None:
            show._p["volume"] = round(max(0.0, min(1.0, float(volume))), 3)
            show._p.pop("volume_ep", None)
        self.save()
        self.notify()

    @staticmethod
    def _target_volume(show: ShowTracker) -> float | None:
        if show.volume_mode == VOLUME_OFF:
            return None
        return show.volume

    async def async_refresh_all(self) -> None:
        await asyncio.gather(*(s.refresh(force=True) for s in self.shows.values()))

    @property
    def queue(self) -> list[ShowTracker]:
        """All shows, main list first."""
        return self.list_queue(LIST_MAIN) + self.list_queue(LIST_SECONDARY)

    def list_queue(self, name: str) -> list[ShowTracker]:
        return [
            self.shows[s] for s in self.order if s in self.shows and self.shows[s].list_name == name
        ]

    # ---- hold / auto-start ------------------------------------------------
    def is_active(self, show: ShowTracker, today: date | None = None) -> bool:
        if show.hold is None:
            return True
        if show.hold == HOLD_MANUAL:
            return False
        plan = self.plans.get(show.subentry_id)
        today = today or dt_util.now().date()
        return bool(plan and plan.start and plan.start <= today)

    def hold_text(self, show: ShowTracker) -> str | None:
        if show.hold == HOLD_MANUAL:
            return "on hold"
        if show.hold != HOLD_AUTO:
            return None
        plan = self.plans.get(show.subentry_id)
        if plan is None or plan.start is None:
            return "auto-start: waiting for the season finale date"
        today = dt_util.now().date()
        if plan.start <= today:
            return None if show._p.get("auto_season") == plan.season else "auto-start: starts tonight"
        if not plan.finale:
            return f"auto-starts {_day(plan.start)}"
        if plan.watch_finale and plan.watch_finale != plan.finale:
            return (
                f"auto-starts {_day(plan.start)} (finale out {_day(plan.finale)}, "
                f"you'll watch it {_day(plan.watch_finale)})"
            )
        return f"auto-starts {_day(plan.start)} (finale {_day(plan.finale)})"

    def set_hold(self, show: ShowTracker, hold: str | None) -> None:
        show._p.pop("auto_season", None)
        show._set(hold=hold)
        self.notify()

    def set_list(self, show: ShowTracker, name: str) -> None:
        show._set(list=None if name == LIST_MAIN else name)
        self.notify()

    @callback
    def plan(self) -> None:
        """Work out auto-start days, list by list, top to bottom."""
        today = dt_util.now().date()
        plans: dict[str, Plan] = {}
        for name in LISTS:
            lanes: list[Lane] = []
            for show in self.list_queue(name):
                if show.data is None:
                    continue
                days, finale_idx = show.episode_days(today, until_finale=show.hold == HOLD_AUTO)
                if show.hold == HOLD_AUTO:
                    nxt = show.next().episode
                    season = nxt.season if nxt else None
                    watch: date | None = None
                    touched = (show.in_progress or show.last_watched or (0, 0))[0] == season
                    if season is not None and show._p.get("auto_season") == season and not touched:
                        show._p.pop("auto_season", None)  # marked but never actually played
                    if show._p.get("auto_season") is not None and show._p["auto_season"] == season:
                        start: date | None = today  # this season already started: keep going
                    elif finale_idx is None:
                        start = None
                    else:
                        start, watch = latest_start(lanes, days, finale_idx, today)
                    plans[show.subentry_id] = Plan(
                        start=start,
                        finale=days[finale_idx] if finale_idx is not None else None,
                        season=season,
                        episodes=(finale_idx + 1) if finale_idx is not None else 0,
                        watch_finale=watch if finale_idx is not None and start else None,
                    )
                    lane_start = start if start is not None else NEVER
                elif show.hold == HOLD_MANUAL:
                    lane_start = NEVER
                else:
                    lane_start = today
                lanes.append(Lane(show.subentry_id, days, lane_start))
        self.plans = plans

    def move(self, sid: str, after: str | None) -> None:
        if sid not in self.order:
            return
        self.order.remove(sid)
        idx = 0 if after is None else self.order.index(after) + 1
        self.order.insert(idx, sid)
        self.save()
        self.notify()

    def show_by_name(self, name: str) -> ShowTracker:
        if name in self.shows:
            return self.shows[name]
        for show in self.shows.values():
            if _same_show(show.title, name):
                return show
        raise HomeAssistantError(f"Unknown show: {name}")

    def up_next(self, name: str = LIST_MAIN) -> tuple[ShowTracker, Next] | None:
        """Highest-priority active show in a list with something to play."""
        for show in self.list_queue(name):
            if not self.is_active(show):
                continue
            nxt = show.next()
            if nxt.status == STATUS_AVAILABLE and nxt.url:
                return show, nxt
        return None

    # ---- tracking -------------------------------------------------------
    @callback
    def _player_changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        changed = self._stop_clock()
        if new is None or new.state not in ("playing", "paused"):
            if changed:
                self.notify()
            return
        attrs = dict(new.attributes)
        for show in self.shows.values():
            show.seen = None
            changed |= show.observe(attrs, new.state == "playing")
        if new.state == "playing":
            blind = [s for s in self.shows.values() if s.seen and s.blind and s.in_progress]
            if len(blind) == 1:
                self._clock = (blind[0].subentry_id, dt_util.utcnow())
        if changed:
            self.notify()

    @callback
    def _stop_clock(self, restart: bool = False) -> bool:
        """Book the playing time so far. Returns True if an episode got marked watched."""
        if self._clock is None:
            return False
        sid, since = self._clock
        now = dt_util.utcnow()
        self._clock = (sid, now) if restart else None
        show = self.shows.get(sid)
        return bool(show and show.add_playtime((now - since).total_seconds()))

    @callback
    def _tick(self, _now: datetime) -> None:
        if self._stop_clock(restart=True):
            self.notify()
        self._learn_volumes()

    @callback
    def _learn_volumes(self) -> None:
        """Volume learning can't wait for a state change: Netflix sends none mid-episode."""
        st = self.hass.states.get(self.media_player)
        if st is None or st.state != "playing":
            return
        attrs = dict(st.attributes)
        pos, dur = attrs.get("media_position"), attrs.get("media_duration")
        since = attrs.get("media_position_updated_at")
        if pos is not None and isinstance(since, datetime):
            pos = pos + (dt_util.utcnow() - since).total_seconds()
        for show in self.shows.values():
            if show.seen and show.volume_mode == VOLUME_AUTO:
                show._learn_volume(show.seen, attrs, pos, dur)

    # ---- playing --------------------------------------------------------
    async def _fresh(self, shows: list[ShowTracker]) -> None:
        stale = [
            s for s in shows
            if s.refreshed is None or dt_util.utcnow() - s.refreshed > timedelta(minutes=30)
        ]
        if stale:
            await asyncio.gather(*(s.refresh(force=True) for s in stale))

    async def async_play_next(
        self, name: str = LIST_MAIN
    ) -> tuple[ShowTracker | None, Next | None]:
        """Walk a list top-down; play the first new episode."""
        async with self._play_lock:
            queue = self.list_queue(name)
            if not queue:
                which = "secondary list" if name == LIST_SECONDARY else "Up Next list"
                await self.async_announce(f"There are no shows in your {which}.")
                return None, None
            await self._fresh(queue)
            self.plan()
            found = self.up_next(name)
            if found is None:
                await self.async_announce(self._nothing_message(queue))
                return None, None
            show, nxt = found
            skipped = [
                s for s in queue[: queue.index(show)]
                if self.is_active(s) and not s.on_break()
            ]
            await self._start(show, nxt, skipped)
            return show, nxt

    async def async_play(self, show: ShowTracker) -> Next:
        """Play the next episode of one specific show."""
        async with self._play_lock:
            await self._fresh([show])
            nxt = show.next()
            if nxt.status != STATUS_AVAILABLE or not nxt.url:
                await self.async_announce(_explain(show, nxt))
                return nxt
            await self._start(show, nxt, [])
            return nxt

    async def _start(self, show: ShowTracker, nxt: Next, skipped: list[ShowTracker]) -> None:
        assert nxt.episode and nxt.url and nxt.service
        notes = []
        notes.extend(self._take_comebacks())
        if skipped:
            names = ", ".join(s.title for s in skipped)
            notes.append(f"No new episode of {names}. Playing {show.title}.")
        if show.hold == HOLD_AUTO:
            show._p["auto_season"] = nxt.episode.season  # keeps it active until the finale
        if show.hold == HOLD_AUTO and show._p.get("auto_announced") != nxt.episode.season:
            show._p["auto_announced"] = nxt.episode.season
            plan = self.plans.get(show.subentry_id)
            if plan and plan.finale and plan.finale > dt_util.now().date():
                when = plan.watch_finale or plan.finale
                notes.append(
                    f"Starting season {nxt.episode.season} of {show.title}. From here there's "
                    f"a new episode every night you get to it, finale {_day_spoken(when)}."
                )
        if nxt.finale == "series":
            notes.append(f"Heads up: this is the series finale of {show.title}.")
        elif nxt.finale == "season":
            notes.append(f"Heads up: this is the season {nxt.episode.season} finale.")
        if notes:
            await self.async_announce(" ".join(notes))
        # The session starts now, so tracking works even if you take over by hand.
        show._set(
            in_progress=nxt.episode.key,
            started_at=dt_util.utcnow(),
            service=nxt.service.technical,
        )
        self.last_started = show.subentry_id
        self.save()
        self.notify()
        try:
            await self._launch(nxt.service, nxt.url, show.title, self._target_volume(show))
        except (HomeAssistantError, TimeoutError) as err:
            _LOGGER.warning("Starting %s on %s failed: %s", show.title, nxt.service.name, err)
            await self._restore_quiet()
            await self.async_announce(
                f"Sorry, I couldn't start {show.title} on {nxt.service.name}. "
                f"It's {nxt.episode.code}, if you want to start it yourself."
            )
            raise HomeAssistantError(f"Couldn't start {show.title}: {err}") from err

    def _nothing_message(self, queue: list[ShowTracker]) -> str:
        upcoming = []
        for show in queue:
            if not self.is_active(show) or show.on_break():
                continue
            nxt = show.next()
            if nxt.status == STATUS_UPCOMING and nxt.episode and nxt.episode.airstamp:
                upcoming.append((nxt.episode.airstamp, show))
        parts = [*self._take_comebacks(), "No new episodes right now."]
        if upcoming:
            stamp, show = min(upcoming, key=lambda x: x[0])
            parts.append(f"Next up: {show.title}, {_when(stamp)}.")
        starts = [
            (plan.start, self.shows[sid]) for sid, plan in self.plans.items()
            if sid in self.shows and self.shows[sid] in queue and plan.start
            and plan.start > dt_util.now().date()
        ]
        if starts:
            day, show = min(starts, key=lambda x: x[0])
            parts.append(f"{show.title} starts {_day_spoken(day)}.")
        if parts[-1] == "No new episodes right now.":
            parts.append("Nothing new has been announced.")
        return " ".join(parts)

    async def _launch(
        self, service: Service, url: str, title: str, volume: float | None = None
    ) -> None:
        state = self.hass.states.get(self.media_player)
        if state is None or state.state == "unavailable":
            raise HomeAssistantError(f"{self.media_player} is unavailable")
        if state.state in ("off", "standby"):
            await self.hass.services.async_call(
                "media_player", "turn_on", {ATTR_ENTITY_ID: self.media_player}, blocking=True
            )
            for _ in range(30):  # wait (max 15 s) until the Apple TV reports it's awake
                await asyncio.sleep(0.5)
                st = self.hass.states.get(self.media_player)
                if st and st.state not in ("off", "standby"):
                    break
            await asyncio.sleep(3)  # home screen + TV input switch settle
        await self.async_run_steps(service.steps, url, title, volume)

    async def async_run_steps(
        self, steps: tuple, url: str, title: str, volume: float | None = None
    ) -> None:
        self._quiet_from = None
        for action, value in steps:
            async with asyncio.timeout(SEARCH_TIMEOUT + 15 if action == "search" else STEP_TIMEOUT):
                await self._step(action, value, url, title, volume)
        if not any(a == "loud" for a, _ in steps) and volume is not None:
            await self._set_volume(volume)

    async def _step(self, action: str, value: Any, url: str, title: str, volume: float | None) -> None:
        if action == "quiet":
            level = await self._current_volume()
            if level is None:
                _LOGGER.warning("The Apple TV reports no volume; can't turn it down for the launch")
            else:
                _LOGGER.info("Quiet launch: volume %s -> 0", level)
                self._quiet_from = level
                await self._set_volume(0)
        elif action == "loud":
            target = volume if volume is not None else self._quiet_from
            self._quiet_from = None
            if target is not None:
                await self._set_volume(target)
        elif action == "url":
            await self.hass.services.async_call(
                "media_player",
                "play_media",
                {
                    ATTR_ENTITY_ID: self.media_player,
                    "media_content_type": "url",
                    "media_content_id": url,
                },
                blocking=True,
            )
        elif action == "wait":
            await asyncio.sleep(float(value))
        elif action == "keys":
            await self._keys(list(value))
        elif action == "quick":
            await self._keys(list(value), delay=QUICK_KEY_DELAY)
        elif action == "double":
            await self._keys([value, value], delay=0)
        elif action == "text":
            await self._type(title)
        elif action == "search":
            await self._search(title, url)

    async def _restore_quiet(self) -> None:
        """After a failed launch: don't leave the TV silent."""
        if self._quiet_from is not None:
            level, self._quiet_from = self._quiet_from, None
            try:
                await self._set_volume(level)
            except HomeAssistantError:
                pass

    async def _current_volume(self) -> float | None:
        """The Apple TV's volume; just after waking it can take a moment to report one."""
        for _ in range(VOLUME_WAIT * 4):
            st = self.hass.states.get(self.media_player)
            if st and (level := st.attributes.get("volume_level")) is not None:
                return float(level)
            await asyncio.sleep(0.25)
        return None

    async def _set_volume(self, level: float) -> None:
        try:
            await self.hass.services.async_call(
                "media_player",
                "volume_set",
                {ATTR_ENTITY_ID: self.media_player, "volume_level": level},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Couldn't set the volume: %s", err)

    def _atv_ids(self) -> tuple[str | None, str | None]:
        """(apple_tv config entry id, keyboard-focus binary sensor) of our Apple TV."""
        reg = er.async_get(self.hass)
        mp = reg.async_get(self.media_player)
        if mp is None:
            return None, None
        focus = None
        if mp.device_id:
            for ent in er.async_entries_for_device(reg, mp.device_id):
                # unique_id is just the device's MAC; the translation key says what it is
                if ent.domain == "binary_sensor" and (
                    ent.translation_key == "keyboard_focused"
                    or "keyboard" in ent.entity_id
                    or "keyboard" in (ent.unique_id or "").lower()
                ):
                    focus = ent.entity_id
        return mp.config_entry_id, focus

    async def _type(self, text: str) -> None:
        """Type into the focused field (it's already there)."""
        entry_id, focus = self._atv_ids()
        if entry_id is None:
            raise HomeAssistantError("Can't find the Apple TV config entry")
        for _ in range(5):
            if focus and not await self._focused(focus, 4):
                continue
            if await self._try_type(entry_id, text):
                return
        raise HomeAssistantError("The search field never took the text")

    async def _search(self, text: str, url: str) -> None:
        """Cold-started Netflix -> search field -> title typed.

        The only safe order: select (past "Who's watching?"), up+left (Search tab),
        then down (activates the on-screen keyboard; only now is the field focused).
        Pressing select after up+left on the profile screen opens the profile editor,
        so a slow start is handled by closing the app and starting over, never by
        extra presses.
        """
        entry_id, focus = self._atv_ids()
        if entry_id is None:
            raise HomeAssistantError("Can't find the Apple TV config entry")
        for attempt, boot in enumerate(SEARCH_BOOT_WAITS):
            if attempt:
                _LOGGER.info("Netflix wasn't ready; restarting it and waiting longer")
                for action, value in CLOSE_FRONT_APP:
                    await self._step(action, value, url, text, None)
                await self._step("url", None, url, text, None)
            await asyncio.sleep(boot)
            await self._keys(["select"])  # "Who's watching?" -> the profile
            await asyncio.sleep(1.5)
            await self._keys(["up", "left"])  # top bar -> Search tab
            await asyncio.sleep(1.5)  # the app takes a moment to catch up
            await self._keys(["down"])  # into the on-screen keyboard: field takes text
            if focus is None:  # no feedback available: type and hope
                await self._try_type(entry_id, text)
                return
            for _ in range(3):  # the field's focus can flicker while the page builds
                if not await self._focused(focus, 4):
                    break
                if await self._try_type(entry_id, text):
                    return
        raise HomeAssistantError("Netflix never showed its search field")

    async def _try_type(self, entry_id: str, text: str) -> bool:
        """Set the text; again a moment later (catches a field that wasn't ready)."""
        try:
            await self.hass.services.async_call(
                "apple_tv", "set_keyboard_text", {"config_entry_id": entry_id, "text": text},
                blocking=True,
            )
        except HomeAssistantError as err:  # focus flickered away between check and call
            _LOGGER.debug("Typing failed: %s", err)
            return False
        await asyncio.sleep(0.5)
        try:
            await self.hass.services.async_call(
                "apple_tv", "set_keyboard_text", {"config_entry_id": entry_id, "text": text},
                blocking=True,
            )
        except HomeAssistantError:
            pass
        return True

    async def _focused(self, focus: str, seconds: float) -> bool:
        for _ in range(int(seconds * 4)):
            st = self.hass.states.get(focus)
            if st and st.state == "on":
                return True
            await asyncio.sleep(0.25)
        return False

    async def _keys(self, keys: list[str], delay: float = 0.4) -> None:
        if not self.remote:
            raise HomeAssistantError("No Apple TV remote entity configured")
        await self.hass.services.async_call(
            "remote",
            "send_command",
            {ATTR_ENTITY_ID: self.remote, "command": keys, "delay_secs": delay},
            blocking=True,
        )

    async def async_announce(self, message: str) -> None:
        _LOGGER.info("%s", message)
        if not self.announce_entity:
            return
        if self.hass.services.has_service("notify", "alexa_media"):
            await self.hass.services.async_call(
                "notify",
                "alexa_media",
                {"message": message, "target": [self.announce_entity], "data": {"type": "tts"}},
                blocking=False,
            )


def _when(stamp: datetime) -> str:
    """Spoken-style: 'today at 14:00', 'tomorrow', 'on Friday', 'on Friday 9 July 2027'."""
    local = dt_util.as_local(stamp)
    days = (local.date() - dt_util.now().date()).days
    if days <= 0:
        return f"today at {local:%H:%M}"
    if days == 1:
        return "tomorrow"
    if days < 7:
        return f"on {local:%A}"
    return f"on {local:%A} {_date(stamp)}"


def _date(stamp: datetime) -> str:
    """'9 July', or '9 July 2027' when it's not within the coming months."""
    local = dt_util.as_local(stamp)
    text = f"{local.day} {local:%B}"
    if (local.date() - dt_util.now().date()).days > 180 or local.year < dt_util.now().year:
        text += f" {local.year}"
    return text


def _day(day: date | None) -> str:
    """'Tue 3 Nov' (with year when far away)."""
    if day is None:
        return "?"
    text = f"{day:%a} {day.day} {day:%b}"
    if (day - dt_util.now().date()).days > 180:
        text += f" {day.year}"
    return text


def _day_spoken(day: date) -> str:
    days = (day - dt_util.now().date()).days
    if days <= 0:
        return "tonight"
    if days == 1:
        return "tomorrow"
    if days < 7:
        return f"on {day:%A}"
    return f"on {day:%A} {day.day} {day:%B}"


def _comeback_text(show: ShowTracker) -> str:
    nxt = show.next()
    ep = nxt.episode
    if nxt.status == STATUS_AVAILABLE and ep:
        return f"{show.title} is back: {ep.code} is out now."
    if ep and ep.airstamp:
        return f"{show.title} is back: {ep.code} comes out {_when(ep.airstamp)}."
    return f"{show.title} has new episodes."


def _real_title(title: str | None) -> bool:
    return bool(title) and _norm(title) not in {"tba", "tbd", "tbc", ""} and not re.fullmatch(
        r"episode \d+", title.strip().lower()
    )


def _explain(show: ShowTracker, nxt: Next) -> str:
    ep = nxt.episode
    name = show.title
    what = f"season {ep.season}, episode {ep.episode}" if ep else ""
    if nxt.status == STATUS_UPCOMING and ep and ep.airstamp:
        return f"The next episode of {name}, {what}, comes out {_when(ep.airstamp)}."
    if nxt.status == STATUS_WAITING:
        return f"You're all caught up on {name}. No new episode has been announced yet."
    if nxt.status == STATUS_FINISHED:
        return f"You've watched every episode of {name}."
    if nxt.status == STATUS_UNAVAILABLE:
        return f"{name} {what} isn't on any of your streaming services."
    return f"I couldn't look up {name} right now."


def last_watched_for(
    show: ShowData, mode: str, next_key: tuple[int, int] | None = None
) -> list[int] | None:
    """Translate a 'where are you' choice into 'last watched' (None = nothing yet).

    beginning: nothing watched · caught_up: everything that's out · latest: all but
    the most recent episode · custom: everything before next_key.
    """
    if mode == "custom" and next_key:
        prev = show.before(next_key)
        return [prev.season, prev.episode] if prev else None
    if mode == "beginning":
        return None
    last = show.last_aired(dt_util.utcnow())
    if last is None:
        return None
    if mode == "caught_up":
        return [last.season, last.episode]
    if mode == "season_start":
        first = next(e for e in show.episodes if e.season == last.season)
        prev = show.before(first.key)
        return [prev.season, prev.episode] if prev else None
    prev = show.before(last.key)  # latest
    return [prev.season, prev.episode] if prev else None


def current_mode(tracker: ShowTracker) -> str:
    """Which 'where are you' choice describes this show right now."""
    if tracker.data is None:
        return "caught_up"
    nxt = tracker.next().episode
    last = tracker.data.last_aired(dt_util.utcnow())
    if nxt is None or (last and nxt.key > last.key):
        return "caught_up"
    if last and nxt.key == last.key:
        return "latest"
    if last and nxt.season == last.season and nxt.key == next(
        e.key for e in tracker.data.episodes if e.season == last.season
    ):
        return "season_start"
    if tracker.data.episodes and nxt.key == tracker.data.episodes[0].key:
        return "beginning"
    return "custom"
