"""Config flow: Apple TV, subscriptions, announcements; shows are subentries."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from .api import Catalog, CatalogError, SearchResult, ShowData
from .const import (
    CONF_ANNOUNCE,
    CONF_COUNTRY,
    CONF_IMDB_ID,
    CONF_JW_ID,
    CONF_LANGUAGE,
    CONF_LAST_WATCHED,
    CONF_MEDIA_PLAYER,
    CONF_NEXT_EPISODE,
    CONF_QUERY,
    CONF_REMOTE,
    CONF_RESULT,
    CONF_START,
    CONF_SUBSCRIPTIONS,
    CONF_TITLE,
    DEFAULT_COUNTRY,
    DEFAULT_LANGUAGE,
    DOMAIN,
    NAME,
    START_BEGINNING,
    START_CAUGHT_UP,
    START_CUSTOM,
    START_LATEST,
    START_SEASON,
    SUBENTRY_SHOW,
)
from .manager import _date, _real_title, current_mode, last_watched_for
from .services_catalog import SERVICES


def _remote_for(hass: HomeAssistant, media_player: str) -> str | None:
    """The remote entity on the same device as the media player."""
    reg = er.async_get(hass)
    mp = reg.async_get(media_player)
    if mp is None or mp.device_id is None:
        return None
    for ent in er.async_entries_for_device(reg, mp.device_id, include_disabled_entities=True):
        if ent.domain == "remote":
            return ent.entity_id
    return None


def _main_schema(defaults: dict[str, Any]) -> vol.Schema:
    services = [SelectOptionDict(value=k, label=s.name) for k, s in SERVICES.items()]
    return vol.Schema(
        {
            vol.Required(CONF_MEDIA_PLAYER, default=defaults.get(CONF_MEDIA_PLAYER)): EntitySelector(
                EntitySelectorConfig(domain="media_player", integration="apple_tv")
            ),
            vol.Required(
                CONF_SUBSCRIPTIONS, default=defaults.get(CONF_SUBSCRIPTIONS, list(SERVICES))
            ): SelectSelector(
                SelectSelectorConfig(
                    options=services, multiple=True, mode=SelectSelectorMode.LIST
                )
            ),
            vol.Optional(
                CONF_ANNOUNCE,
                description={"suggested_value": defaults.get(CONF_ANNOUNCE)},
            ): EntitySelector(EntitySelectorConfig(domain="media_player")),
            vol.Required(CONF_COUNTRY, default=defaults.get(CONF_COUNTRY, DEFAULT_COUNTRY)): str,
            vol.Required(
                CONF_LANGUAGE, default=defaults.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
            ): str,
        }
    )


def _clean(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    data = {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}
    data[CONF_COUNTRY] = data[CONF_COUNTRY].upper()
    data[CONF_LANGUAGE] = data[CONF_LANGUAGE].lower()
    data[CONF_REMOTE] = _remote_for(hass, data[CONF_MEDIA_PLAYER])
    return data


def _validate(data: dict[str, Any]) -> dict[str, str]:
    if not data[CONF_REMOTE]:
        return {"base": "no_remote"}
    if not data.get(CONF_SUBSCRIPTIONS):
        return {"base": "no_subscriptions"}
    return {}


class UpNextConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(self.hass, user_input)
            if not (errors := _validate(data)):
                return self.async_create_entry(title=NAME, data=data)
        return self.async_show_form(
            step_id="user", data_schema=_main_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return UpNextOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_SHOW: ShowFlow}


class UpNextOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(self.hass, user_input)
            if not (errors := _validate(data)):
                return self.async_create_entry(data=data)
            current = user_input
        return self.async_show_form(
            step_id="init", data_schema=_main_schema(current), errors=errors
        )


def _episode_options(show: ShowData) -> list[SelectOptionDict]:
    now = dt_util.utcnow()
    opts = []
    for ep in show.episodes:
        label = ep.code + (f" · {ep.title}" if _real_title(ep.title) else "")
        if ep.airstamp and ep.airstamp > now and not ep.links:
            label += f" (out {_date(ep.airstamp)})"
        opts.append(SelectOptionDict(value=f"{ep.season}:{ep.episode}", label=label))
    return opts


class ShowFlow(ConfigSubentryFlow):
    """Add a show: search -> pick -> where you are (-> which episode)."""

    def __init__(self) -> None:
        self._results: list[SearchResult] = []
        self._picked: SearchResult | None = None
        self._show: ShowData | None = None

    def _opts(self) -> dict[str, Any]:
        entry = self._get_entry()
        return {**entry.data, **entry.options}

    def _catalog(self) -> Catalog:
        opts = self._opts()
        return Catalog(
            async_get_clientsession(self.hass),
            opts.get(CONF_COUNTRY, DEFAULT_COUNTRY),
            opts.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        )

    # -- add ---------------------------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._results = await self._catalog().search(user_input[CONF_QUERY].strip())
            except CatalogError:
                errors["base"] = "cannot_connect"
            else:
                if self._results:
                    return await self.async_step_pick()
                errors["base"] = "no_results"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_QUERY): TextSelector()}),
            errors=errors,
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            picked = next(r for r in self._results if r.jw_id == user_input[CONF_RESULT])
            for sub in self._get_entry().subentries.values():
                if sub.data.get(CONF_JW_ID) == picked.jw_id:
                    return self.async_abort(reason="already_added")
            try:
                self._show = await self._catalog().show(picked.jw_id)
            except CatalogError:
                errors["base"] = "cannot_connect"
            else:
                self._picked = picked
                return await self.async_step_progress()
        options = [SelectOptionDict(value=r.jw_id, label=r.label) for r in self._results]
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_RESULT, default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
            errors=errors,
        )

    def _progress_form(
        self, step_id: str, title: str, current: str = START_CAUGHT_UP
    ) -> SubentryFlowResult:
        subs = set(self._opts().get(CONF_SUBSCRIPTIONS, list(SERVICES)))
        on = {k for ep in (self._show.episodes if self._show else []) for k in ep.links}
        on |= set(self._show.show_links) if self._show else set()
        mine = [SERVICES[k].name for k in sorted(on) if k in subs and k in SERVICES]
        where = ", ".join(mine) if mine else "none of your streaming services (yet)"
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_START, default=current): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                START_CAUGHT_UP, START_LATEST, START_SEASON, START_BEGINNING,
                                START_CUSTOM,
                            ],
                            translation_key="start",
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={"title": title, "where": where},
        )

    def _last_watched_for(self, start: str) -> list[int] | None:
        return last_watched_for(self._show, start) if self._show else None

    async def async_step_progress(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        assert self._picked is not None
        if user_input is not None:
            if user_input[CONF_START] == START_CUSTOM and self._show and self._show.episodes:
                return await self.async_step_episode()
            return self._create(self._last_watched_for(user_input[CONF_START]))
        return self._progress_form("progress", self._picked.title)

    async def async_step_episode(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        assert self._show is not None
        if user_input is not None:
            season, episode = (int(x) for x in user_input[CONF_NEXT_EPISODE].split(":"))
            prev = self._show.before((season, episode))
            last = [prev.season, prev.episode] if prev else None
            if self.source == "reconfigure":
                return await self._reconfigure_done(last)
            return self._create(last)
        options = _episode_options(self._show)
        default_ep = self._show.after(None)
        if (last := self._show.last_aired(dt_util.utcnow())) is not None:
            default_ep = last
        if self.source == "reconfigure":
            manager = getattr(self._get_entry(), "runtime_data", None)
            show = manager.shows.get(self._get_reconfigure_subentry().subentry_id) if manager else None
            if show and (cur := show.next().episode):
                default_ep = cur
        return self.async_show_form(
            step_id="episode",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NEXT_EPISODE,
                        default=f"{default_ep.season}:{default_ep.episode}" if default_ep else None,
                    ): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
            description_placeholders={"title": self._show.title},
        )

    def _create(self, last_watched: list[int] | None) -> SubentryFlowResult:
        assert self._picked is not None
        return self.async_create_entry(
            title=self._picked.title,
            data={
                CONF_TITLE: self._picked.title,
                CONF_JW_ID: self._picked.jw_id,
                CONF_IMDB_ID: self._picked.imdb_id,
                CONF_LAST_WATCHED: last_watched,
            },
        )

    # -- reconfigure: "where am I" -------------------------------------------
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        if self._show is None:
            try:
                self._show = await self._catalog().show(sub.data[CONF_JW_ID])
            except CatalogError:
                return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            if user_input[CONF_START] == START_CUSTOM and self._show.episodes:
                return await self.async_step_episode()
            return await self._reconfigure_done(self._last_watched_for(user_input[CONF_START]))
        return self._progress_form("reconfigure", sub.data[CONF_TITLE], self._current_mode(sub))

    def _current_mode(self, sub: Any) -> str:
        manager = getattr(self._get_entry(), "runtime_data", None)
        show = manager.shows.get(sub.subentry_id) if manager else None
        return current_mode(show) if show else START_CAUGHT_UP

    async def _reconfigure_done(self, last: list[int] | None) -> SubentryFlowResult:
        entry = self._get_entry()
        sub = self._get_reconfigure_subentry()
        manager = getattr(entry, "runtime_data", None)
        if manager is not None and (show := manager.shows.get(sub.subentry_id)):
            show.set_last_watched(tuple(last) if last else None)
            await manager.async_save_now()
        return self.async_update_and_abort(
            entry, sub, data={**sub.data, CONF_LAST_WATCHED: last}
        )
