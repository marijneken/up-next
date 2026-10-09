"""Up Next: play the next episode of your favorite show on an Apple TV."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN
from .manager import UpNextManager
from .panel import async_setup_panel

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]

type UpNextConfigEntry = ConfigEntry[UpNextManager]

ATTR_SHOW = "show"
SCHEMA_PLAY = vol.Schema(
    {
        vol.Optional(ATTR_SHOW): cv.string,
        vol.Optional("list", default="main"): vol.In(["main", "secondary"]),
    }
)
SCHEMA_SHOW = vol.Schema({vol.Required(ATTR_SHOW): cv.string})
SCHEMA_SET = vol.Schema(
    {
        vol.Required(ATTR_SHOW): cv.string,
        vol.Required("season"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required("episode"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)


def _manager(hass: HomeAssistant) -> UpNextManager:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    raise HomeAssistantError("Up Next is not set up")


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    await async_setup_panel(hass)
    async def play(call: ServiceCall) -> ServiceResponse:
        mgr = _manager(hass)
        if name := call.data.get(ATTR_SHOW):
            show = mgr.show_by_name(name)
            nxt = await mgr.async_play(show)
        else:
            show, nxt = await mgr.async_play_next(call.data["list"])
        return {
            "show": show.title if show else None,
            "status": nxt.status if nxt else "nothing_new",
            "episode": nxt.episode.code if nxt and nxt.episode else None,
            "service": nxt.service.name if nxt and nxt.service else None,
        }

    async def set_next(call: ServiceCall) -> None:
        mgr = _manager(hass)
        show = mgr.show_by_name(call.data.get(ATTR_SHOW))
        show.set_next(call.data["season"], call.data["episode"])
        mgr.notify()

    async def mark_watched(call: ServiceCall) -> None:
        mgr = _manager(hass)
        show = mgr.show_by_name(call.data.get(ATTR_SHOW))
        nxt = show.next()
        if nxt.episode:
            show.mark_watched(nxt.episode.key)
        mgr.notify()

    async def refresh(call: ServiceCall) -> None:
        await _manager(hass).async_refresh_all()

    hass.services.async_register(
        DOMAIN, "play", play, SCHEMA_PLAY, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, "set_next_episode", set_next, SCHEMA_SET)
    hass.services.async_register(DOMAIN, "mark_watched", mark_watched, SCHEMA_SHOW)
    hass.services.async_register(DOMAIN, "refresh", refresh)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: UpNextConfigEntry) -> bool:
    manager = UpNextManager(hass, entry)
    manager.signature = config_signature(entry)
    entry.runtime_data = manager  # before setup, so the panel can already see it
    await manager.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    manager.notify()  # tell open panels the (re)loaded state
    return True


async def _reload(hass: HomeAssistant, entry: UpNextConfigEntry) -> None:
    # Up Next renames show entries to show their status; that must not cause a reload.
    if config_signature(entry) == entry.runtime_data.signature:
        return
    await hass.config_entries.async_reload(entry.entry_id)


def config_signature(entry: ConfigEntry) -> str:
    """Everything that matters for setup (not the cosmetic subentry titles)."""
    return repr(
        (
            sorted(entry.data.items()),
            sorted(entry.options.items()),
            sorted((sid, sorted(sub.data.items())) for sid, sub in entry.subentries.items()),
        )
    )


async def async_unload_entry(hass: HomeAssistant, entry: UpNextConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_unload()
    return ok
