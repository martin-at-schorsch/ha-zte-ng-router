from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZteActionSwitchDef:
    key: str
    name: str
    icon: str
    state_key: str
    on_value: str
    off_value: str
    get_path: Callable[[dict[str, Any]], dict[str, Any]]
    turn_on: dict[str, Any]
    turn_off: dict[str, Any]


SWITCH_DEFS: list[ZteActionSwitchDef] = [
    ZteActionSwitchDef(
        key="odu_led",
        name="LED",
        icon="mdi:led-on",
        state_key="switch",
        on_value="1",
        off_value="0",
        # where to read state from coordinator.data
        get_path=lambda data: data.get("odu_led", {}),
        turn_on={
            "service": "zwrt_led",
            "method": "set_ODU_switch_state",
            "params": {"switch": "1", "offtime": "15"},
        },
        turn_off={
            "service": "zwrt_led",
            "method": "set_ODU_switch_state",
            "params": {"switch": "0", "offtime": "15"},
        },
    ),
    ZteActionSwitchDef(
        key="wifi_master",
        name="WiFi",
        icon="mdi:wifi",
        state_key="wifi_onoff",
        on_value="1",
        off_value="0",
        # master state from zwrt_wlan/report (already stored as coordinator.data["wlan"])
        get_path=lambda data: data.get("wlan", {}),
        turn_on={
            "service": "zwrt_wlan",
            "method": "set",
            "params": {"zte_mbb": {"wifi_onoff": "1"}},
        },
        turn_off={
            "service": "zwrt_wlan",
            "method": "set",
            "params": {"zte_mbb": {"wifi_onoff": "0"}},
        },
    ),
    ZteActionSwitchDef(
        key="wifi_main_2g",
        name="WiFi 2 GHz",
        icon="mdi:wifi",
        state_key="disabled",
        # UCI uses disabled=0 (enabled) and disabled=1 (disabled)
        on_value="0",
        off_value="1",
        get_path=lambda data: data.get("wifi_main_2g", {}),
        turn_on={
            "service": "zwrt_wlan",
            "method": "set",
            "params": {"main_2g": {"disabled": "0"}},
        },
        turn_off={
            "service": "zwrt_wlan",
            "method": "set",
            "params": {"main_2g": {"disabled": "1"}},
        },
    ),
    ZteActionSwitchDef(
        key="wifi_main_5g",
        name="WiFi 5 GHz",
        icon="mdi:wifi",
        state_key="disabled",
        # UCI uses disabled=0 (enabled) and disabled=1 (disabled)
        on_value="0",
        off_value="1",
        get_path=lambda data: data.get("wifi_main_5g", {}),
        turn_on={
            "service": "zwrt_wlan",
            "method": "set",
            "params": {"main_5g": {"disabled": "0"}},
        },
        turn_off={
            "service": "zwrt_wlan",
            "method": "set",
            "params": {"main_5g": {"disabled": "1"}},
        },
    ),
    # weitere Switches einfach hier anhängen
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    coordinator = data["coordinator"]
    name = data.get("name", "ZTE Router")

    async_add_entities(
        [
            ZteActionSwitch(coordinator, api, entry, name, switch_def)
            for switch_def in SWITCH_DEFS
        ]
    )


class ZteActionSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        api,
        entry: ConfigEntry,
        device_name: str,
        switch_def: ZteActionSwitchDef,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._def = switch_def

        self._attr_name = switch_def.name
        self._attr_icon = switch_def.icon
        self._attr_unique_id = f"{entry.entry_id}_{switch_def.key}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name,
            manufacturer="ZTE",
        )

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}

        # If WiFi master is OFF, force band switches to show OFF
        if self._def.key in ("wifi_main_2g", "wifi_main_5g"):
            wifi_onoff = (data.get("wlan") or {}).get("wifi_onoff")
            if str(wifi_onoff) != "1":
                return False

        node = self._def.get_path(data) or {}
        value = node.get(self._def.state_key)
        return str(value) == self._def.on_value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        return self._def.get_path(data)


    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.info("Turning ON ZTE switch: %s", self._def.key)
        ok = await self._api.async_execute_action_def(self._def.turn_on)
        if ok:
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.warning("Failed to turn ON ZTE switch: %s", self._def.key)

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.info("Turning OFF ZTE switch: %s", self._def.key)
        ok = await self._api.async_execute_action_def(self._def.turn_off)
        if ok:
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.warning("Failed to turn OFF ZTE switch: %s", self._def.key)
