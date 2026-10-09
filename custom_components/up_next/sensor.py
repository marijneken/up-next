"""'Up next' sensor: what the play button would start right now."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UpNextConfigEntry
from .entity import UpNextEntity
from .manager import UpNextManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UpNextConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [UpNextSensor(entry.runtime_data), UpNextSensor(entry.runtime_data, "secondary")]
    )


class UpNextSensor(UpNextEntity, SensorEntity):
    _attr_translation_key = "up_next"
    _attr_icon = "mdi:television-play"

    def __init__(self, manager: UpNextManager, list_name: str = "main") -> None:
        super().__init__(manager, "up_next" if list_name == "main" else f"up_next_{list_name}")
        self._list = list_name
        if list_name != "main":
            self._attr_translation_key = f"up_next_{list_name}"

    @property
    def native_value(self) -> str:
        found = self.manager.up_next(self._list)
        if not found:
            return "Nothing new"
        show, nxt = found
        assert nxt.episode
        return f"{show.title} {nxt.episode.code}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        found = self.manager.up_next(self._list)
        attrs: dict[str, Any] = {}
        if found:
            show, nxt = found
            assert nxt.episode and nxt.service
            attrs.update(
                show=show.title,
                season=nxt.episode.season,
                episode=nxt.episode.episode,
                episode_title=nxt.episode.title,
                service=nxt.service.name,
                finale=nxt.finale,
            )
        attrs["queue"] = [
            {"show": s.title, "status": s.next().status, "next": s.status_line()}
            for s in self.manager.list_queue(self._list)
            if not s.on_break()
        ]
        return attrs
