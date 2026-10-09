"""Sidebar panel: registration + websocket API it talks to."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .api import CatalogError
from .const import (
    CONF_IMDB_ID,
    CONF_JW_ID,
    CONF_LAST_WATCHED,
    CONF_TITLE,
    DOMAIN,
    SIGNAL_UPDATED,
    SUBENTRY_SHOW,
)
from .manager import (
    STATUS_AVAILABLE,
    VOLUME_MODES,
    ShowTracker,
    UpNextManager,
    _date,
    _real_title,
    current_mode,
    last_watched_for,
)
from .services_catalog import SERVICES

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/up_next_static"
PANEL_URL = "up-next"
MODES = ["beginning", "caught_up", "latest", "season_start", "custom"]
HOLDS = ["none", "manual", "auto"]
LIST_NAMES = ["main", "secondary"]


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register the websocket API, serve the panel JS and add 'Up Next' to the sidebar."""
    if hass.data.get(f"{DOMAIN}_panel"):
        return
    hass.data[f"{DOMAIN}_panel"] = True
    for cmd in (ws_subscribe, ws_move, ws_remove, ws_play, ws_search, ws_add, ws_episodes,
                ws_set_progress, ws_set_hold, ws_set_list, ws_set_volume):
        websocket_api.async_register_command(hass, cmd)
    if hass.http is None:  # e.g. tests without a web server
        return
    www = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(www), cache_headers=False)]
    )
    version = (www / "up-next-panel.js").stat().st_mtime_ns
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL,
            webcomponent_name="up-next-panel",
            sidebar_title="Up Next",
            sidebar_icon="mdi:television-play",
            module_url=f"{URL_BASE}/up-next-panel.js?v={version}",
            require_admin=False,
        )
    except Exception:  # noqa: BLE001 - frontend not loaded; the integration still works
        _LOGGER.exception("Could not add the Up Next panel to the sidebar")


def _manager(hass: HomeAssistant) -> UpNextManager | None:
    # Not only "loaded": while the entry reloads (e.g. after adding a show) the
    # manager is already attached and the panel must keep working.
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (mgr := getattr(entry, "runtime_data", None)) is not None:
            return mgr
    return None


def _show_dict(show: ShowTracker) -> dict[str, Any]:
    nxt = show.next()
    ep = nxt.episode
    mgr = show.manager
    plan = mgr.plans.get(show.subentry_id)
    return {
        "id": show.subentry_id,
        "list": show.list_name,
        "hold": show.hold or "none",
        "active": mgr.is_active(show),
        "hold_text": mgr.hold_text(show),
        "on_break": show.on_break(),
        "volume_mode": show.volume_mode,
        "volume": show.volume,
        "volume_from": "S{:02d}E{:02d}".format(*show._p["volume_ep"]) if show._p.get("volume_ep") else None,
        "plan": {
            "start": plan.start.isoformat() if plan and plan.start else None,
            "finale": plan.finale.isoformat() if plan and plan.finale else None,
            "episodes": plan.episodes if plan else 0,
            "watch_finale": plan.watch_finale.isoformat() if plan and plan.watch_finale else None,
        } if plan else None,
        "title": show.title,
        "poster": show.data.poster if show.data else None,
        "status": nxt.status,
        "line": show.status_line(),
        "short": show.short_status() if show.data else "loading…",
        "mode": current_mode(show),
        "playable": nxt.status == STATUS_AVAILABLE and bool(nxt.url),
        "short_base": show._short_status() if show.data else "loading…",
        "service": nxt.service.name if nxt.service else (
            SERVICES[show.service_key].name if show.service_key in SERVICES else None
        ),
        "next": {
            "code": ep.code,
            "key": f"{ep.season}:{ep.episode}",
            "title": ep.title if _real_title(ep.title) else None,
            "air": ep.airstamp.isoformat() if ep.airstamp else None,
            "air_text": _date(ep.airstamp) if ep.airstamp else None,
            "finale": nxt.finale,
        } if ep else None,
        "last_watched": "S{:02d}E{:02d}".format(*show.last_watched) if show.last_watched else None,
        "in_progress": "S{:02d}E{:02d}".format(*show.in_progress) if show.in_progress else None,
        "error": show.error,
    }


def _snapshot(hass: HomeAssistant) -> dict[str, Any]:
    mgr = _manager(hass)
    if mgr is None:
        return {"ready": False, "shows": []}
    main = mgr.up_next("main")
    secondary = mgr.up_next("secondary")
    return {
        "ready": True,
        "shows": [_show_dict(s) for s in mgr.queue],
        "up_next": main[0].subentry_id if main else None,
        "up_next_secondary": secondary[0].subentry_id if secondary else None,
        "subscriptions": [SERVICES[s].name for s in mgr.subscriptions],
    }


@websocket_api.websocket_command({vol.Required("type"): "up_next/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Send the queue now and whenever it changes."""
    msg_id = msg["id"]

    @callback
    def send() -> None:
        connection.send_message(websocket_api.event_message(msg_id, _snapshot(hass)))

    unsubs = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        unsubs.append(async_dispatcher_connect(hass, f"{SIGNAL_UPDATED}_{entry.entry_id}", send))

    @callback
    def unsub() -> None:
        for u in unsubs:
            u()

    connection.subscriptions[msg_id] = unsub
    connection.send_result(msg_id)
    send()


def _require(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> UpNextManager | None:
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Up Next is not set up")
    return mgr


@websocket_api.websocket_command(
    {vol.Required("type"): "up_next/move", vol.Required("show_id"): str, vol.Optional("after"): vol.Any(str, None)}
)
@callback
def ws_move(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if mgr := _require(hass, connection, msg):
        mgr.move(msg["show_id"], msg.get("after"))
        connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "up_next/remove", vol.Required("show_id"): str})
@callback
def ws_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if mgr := _require(hass, connection, msg):
        if msg["show_id"] in mgr.entry.subentries:
            hass.config_entries.async_remove_subentry(mgr.entry, msg["show_id"])
        connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "up_next/play",
        vol.Optional("show_id"): str,
        vol.Optional("list", default="main"): vol.In(LIST_NAMES),
    }
)
@websocket_api.async_response
async def ws_play(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if mgr := _require(hass, connection, msg):
        if show_id := msg.get("show_id"):
            show = mgr.shows.get(show_id)
            if show is None:
                connection.send_error(msg["id"], "not_found", "Unknown show")
                return
            nxt = await mgr.async_play(show)
            connection.send_result(msg["id"], {"status": nxt.status})
        else:
            show, nxt = await mgr.async_play_next(msg["list"])
            connection.send_result(
                msg["id"], {"status": nxt.status if nxt else "nothing_new",
                            "show": show.title if show else None}
            )


@websocket_api.websocket_command({vol.Required("type"): "up_next/search", vol.Required("query"): str})
@websocket_api.async_response
async def ws_search(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not (mgr := _require(hass, connection, msg)):
        return
    try:
        results = await mgr.catalog.search(msg["query"].strip())
    except CatalogError as err:
        connection.send_error(msg["id"], "catalog", str(err))
        return
    have = {sub.data.get(CONF_JW_ID) for sub in mgr.entry.subentries.values()}
    subs = set(mgr.subscriptions)
    connection.send_result(
        msg["id"],
        [
            {
                "jw_id": r.jw_id,
                "title": r.title,
                "year": r.year,
                "imdb_id": r.imdb_id,
                "poster": r.poster,
                "services": sorted(r.services.values()),
                "mine": sorted(SERVICES[k].name for k in r.services if k in subs),
                "added": r.jw_id in have,
            }
            for r in results
        ],
    )


def _episode_list(show_data: Any) -> list[dict[str, Any]]:
    return [
        {
            "key": f"{e.season}:{e.episode}",
            "label": e.code + (f" · {e.title}" if _real_title(e.title) else ""),
            "out": bool(e.links) or bool(e.airstamp and e.airstamp <= dt_util.utcnow()),
            "air_text": _date(e.airstamp) if e.airstamp else None,
        }
        for e in show_data.episodes
    ]


@websocket_api.websocket_command(
    {vol.Required("type"): "up_next/episodes", vol.Optional("show_id"): str, vol.Optional("jw_id"): str}
)
@websocket_api.async_response
async def ws_episodes(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not (mgr := _require(hass, connection, msg)):
        return
    show = mgr.shows.get(msg.get("show_id", ""))
    if show and show.data:
        connection.send_result(msg["id"], _episode_list(show.data))
        return
    try:
        jw_id = msg.get("jw_id") or (show.jw_id if show else "")
        data = await mgr.catalog.show(jw_id)
    except CatalogError as err:
        connection.send_error(msg["id"], "catalog", str(err))
        return
    connection.send_result(msg["id"], _episode_list(data))


def _key(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    season, episode = value.split(":")
    return int(season), int(episode)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "up_next/add",
        vol.Required("jw_id"): str,
        vol.Required("title"): str,
        vol.Optional("imdb_id"): vol.Any(str, None),
        vol.Required("mode"): vol.In(MODES),
        vol.Optional("next"): vol.Any(str, None),
        vol.Optional("list", default="main"): vol.In(LIST_NAMES),
        vol.Optional("hold", default="none"): vol.In(HOLDS),
    }
)
@websocket_api.async_response
async def ws_add(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not (mgr := _require(hass, connection, msg)):
        return
    if any(s.data.get(CONF_JW_ID) == msg["jw_id"] for s in mgr.entry.subentries.values()):
        connection.send_error(msg["id"], "exists", "That show is already in your queue")
        return
    try:
        data = await mgr.catalog.show(msg["jw_id"])
    except CatalogError as err:
        connection.send_error(msg["id"], "catalog", str(err))
        return
    last = last_watched_for(data, msg["mode"], _key(msg.get("next")))
    sub = ConfigSubentry(
        data={
            CONF_TITLE: msg["title"],
            CONF_JW_ID: msg["jw_id"],
            CONF_IMDB_ID: msg.get("imdb_id"),
            CONF_LAST_WATCHED: last,
        },
        subentry_type=SUBENTRY_SHOW,
        title=msg["title"],
        unique_id=None,
    )
    # list / hold live in Up Next's own storage; seed them before the entry reloads
    extra: dict[str, Any] = {}
    if msg["list"] != "main":
        extra["list"] = msg["list"]
    if msg["hold"] != "none":
        extra["hold"] = msg["hold"]
    if extra:
        mgr.progress.setdefault(sub.subentry_id, {}).update(extra)
        await mgr.async_save_now()
    hass.config_entries.async_add_subentry(mgr.entry, sub)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "up_next/set_progress",
        vol.Required("show_id"): str,
        vol.Required("mode"): vol.In(MODES),
        vol.Optional("next"): vol.Any(str, None),
    }
)
@websocket_api.async_response
async def ws_set_progress(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not (mgr := _require(hass, connection, msg)):
        return
    show = mgr.shows.get(msg["show_id"])
    if show is None or show.data is None:
        connection.send_error(msg["id"], "not_found", "Unknown show (or catalog not loaded)")
        return
    last = last_watched_for(show.data, msg["mode"], _key(msg.get("next")))
    show.set_last_watched(tuple(last) if last else None)
    await mgr.async_save_now()
    mgr.notify()
    connection.send_result(msg["id"], _show_dict(show))


@websocket_api.websocket_command(
    {vol.Required("type"): "up_next/set_hold", vol.Required("show_id"): str, vol.Required("hold"): vol.In(HOLDS)}
)
@websocket_api.async_response
async def ws_set_hold(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not (mgr := _require(hass, connection, msg)):
        return
    if (show := mgr.shows.get(msg["show_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Unknown show")
        return
    mgr.set_hold(show, None if msg["hold"] == "none" else msg["hold"])
    await mgr.async_save_now()
    connection.send_result(msg["id"], _show_dict(show))


@websocket_api.websocket_command(
    {vol.Required("type"): "up_next/set_list", vol.Required("show_id"): str, vol.Required("list"): vol.In(LIST_NAMES)}
)
@websocket_api.async_response
async def ws_set_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not (mgr := _require(hass, connection, msg)):
        return
    if (show := mgr.shows.get(msg["show_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Unknown show")
        return
    mgr.set_list(show, msg["list"])
    await mgr.async_save_now()
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "up_next/set_volume",
        vol.Required("show_id"): str,
        vol.Required("mode"): vol.In(VOLUME_MODES),
        vol.Optional("volume"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
    }
)
@websocket_api.async_response
async def ws_set_volume(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not (mgr := _require(hass, connection, msg)):
        return
    if (show := mgr.shows.get(msg["show_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Unknown show")
        return
    mgr.set_volume(show, msg["mode"], msg.get("volume"))
    await mgr.async_save_now()
    connection.send_result(msg["id"], _show_dict(show))
