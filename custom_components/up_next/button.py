"""'Play up next' button (expose this one to Alexa)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
        [PlayUpNextButton(entry.runtime_data), PlaySecondaryButton(entry.runtime_data)]
    )


class PlayUpNextButton(UpNextEntity, ButtonEntity):
    _attr_translation_key = "play_up_next"
    _attr_icon = "mdi:play-circle"

    def __init__(self, manager: UpNextManager) -> None:
        # unique id kept from v0.1 so the Alexa label / entity id survive
        super().__init__(manager, "play_favorite")

    async def async_press(self) -> None:
        await self.manager.async_play_next()


class PlaySecondaryButton(UpNextEntity, ButtonEntity):
    _attr_translation_key = "play_secondary"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, manager: UpNextManager) -> None:
        super().__init__(manager, "play_secondary")

    async def async_press(self) -> None:
        await self.manager.async_play_next("secondary")
