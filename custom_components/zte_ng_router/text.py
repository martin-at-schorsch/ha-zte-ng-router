"""Text platform for ZTE router integration (Cell Lock inputs)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.text import TextEntity, TextEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .zte_api import ZteRouterApi

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ZteCellLockTextDescription(TextEntityDescription):
    """Description for a ZTE Cell Lock text entity."""

    kind: str  # "4g" or "5g"


TEXTS: tuple[ZteCellLockTextDescription, ...] = (
    ZteCellLockTextDescription(
        key="cell_lock_4g_text",
        name="Cell Lock 4G",
        icon="mdi:cellphone-signal",
        entity_category=EntityCategory.CONFIG,
        kind="4g",
    ),
    ZteCellLockTextDescription(
        key="cell_lock_5g_text",
        name="Cell Lock 5G",
        icon="mdi:cellphone-signal",
        entity_category=EntityCategory.CONFIG,
        kind="5g",
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    coordinator = data["coordinator"]
    name = data.get("name", "ZTE Router")

    async_add_entities([
        ZteCellLockTextEntity(hass, coordinator, api, entry, name, desc)
        for desc in TEXTS
    ])


class ZteCellLockTextEntity(CoordinatorEntity, TextEntity):
    """Editable cell lock text input.

    Behavior:
    - If the router lock is active, the value shown is the router's lock string.
    - If the lock is NOT active, the value shown is the last user-entered value (if any),
      otherwise a suggestion from current serving cell.

    This matches the UX where a user types a value, then flips the switch to apply it.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator,
        api: ZteRouterApi,
        entry: ConfigEntry,
        device_name: str,
        description: ZteCellLockTextDescription,
    ) -> None:
        super().__init__(coordinator)
        self.hass = hass
        self.entity_description = description
        self._api = api
        self._entry_id = entry.entry_id
        self._kind = description.kind  # "4g" or "5g"

        # Persist last user-entered value in memory (until HA restart)
        self._user_value: str | None = None

        # Unique IDs must match what switch.py expects
        self._attr_unique_id = f"{entry.entry_id}_cell_lock_{self._kind}_text"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name,
            manufacturer="ZTE",
        )

        # TextEntity settings
        self._attr_mode = "text"
        self._attr_native_min = 0
        self._attr_native_max = 32

        # Enforce exact format
        if self._kind == "4g":
            # PCI,EARFCN
            self._attr_pattern = r"^[0-9]+,[0-9]+$"
        else:
            # PCI,ARFCN,BAND
            self._attr_pattern = r"^[0-9]+,[0-9]+,[0-9]+$"

    def _netinfo(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        node = data.get("netinfo")
        return node if isinstance(node, dict) else {}

    @property
    def native_value(self) -> str:
        netinfo = self._netinfo()

        if self._kind == "4g":
            if ZteRouterApi.is_4g_cell_lock_active(netinfo):
                return ZteRouterApi.get_4g_cell_lock_value(netinfo)
            return (
                self._user_value
                or ZteRouterApi.suggest_4g_cell_lock_text(netinfo)
                or ZteRouterApi.get_4g_cell_lock_value(netinfo)
            )

        # 5g
        if ZteRouterApi.is_5g_cell_lock_active(netinfo):
            return ZteRouterApi.get_5g_cell_lock_value(netinfo)
        return (
            self._user_value
            or ZteRouterApi.suggest_5g_cell_lock_text(netinfo)
            or ZteRouterApi.get_5g_cell_lock_value(netinfo)
        )

    async def async_set_value(self, value: str) -> None:
        value = (value or "").strip()

        # Validate format early (raises ValueError)
        if self._kind == "4g":
            ZteRouterApi.parse_4g_cell_lock_input(value)
        else:
            ZteRouterApi.parse_5g_cell_lock_input(value)

        # Store as last user-entered value (switch applies it)
        self._user_value = value

        # Trigger UI update
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        netinfo = self._netinfo()

        if self._kind == "4g":
            return {
                "lock_lte_cell": ZteRouterApi.get_4g_cell_lock_value(netinfo),
                "lock_active": ZteRouterApi.is_4g_cell_lock_active(netinfo),
                "user_value": self._user_value,
                "suggested": ZteRouterApi.suggest_4g_cell_lock_text(netinfo),
            }

        return {
            "lock_nr_cell": ZteRouterApi.get_5g_cell_lock_value(netinfo),
            "lock_active": ZteRouterApi.is_5g_cell_lock_active(netinfo),
            "user_value": self._user_value,
            "suggested": ZteRouterApi.suggest_5g_cell_lock_text(netinfo),
        }
