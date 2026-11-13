"""Diagnostics for Mi Fitness: safe to attach to a GitHub issue."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MiFitnessConfigEntry
from .const import (
    CONF_API_DEVICE_SUFFIX,
    CONF_C_USER_ID,
    CONF_LOGIN_DEVICE_ID,
    CONF_PASS_TOKEN,
    CONF_PASSWORD,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    CONF_USERNAME,
)

TO_REDACT = {
    CONF_PASSWORD,
    CONF_PASS_TOKEN,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USERNAME,
    CONF_USER_ID,
    CONF_C_USER_ID,
    CONF_LOGIN_DEVICE_ID,
    CONF_API_DEVICE_SUFFIX,
    "unique_id",
    "title",
}

# Bulky per-reading data: summarised by size instead of dumped
_SUMMARISE = {"sleep_stages", "steps_hourly", "sleep_history_14d",
              "sleep_sessions_30d", "steps_history_14d", "stand_hours_list"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MiFitnessConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = dict(coordinator.data or {})
    sensor_data = {
        k: (f"<{len(v)} items>" if k in _SUMMARISE and isinstance(v, list) else v)
        for k, v in data.items()
    }
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "watermark": coordinator._watermark,
            "day_watermark": coordinator._day_wm,
            "fetch_errors": coordinator._fetch_errors,
            "protocol_errors": coordinator._protocol_errors,
            "sleep_sessions": len(coordinator._sleep_sessions),
            "step_days": sorted(coordinator._step_buckets),
            "stat_metrics": {
                metric: len(hours) for metric, hours in coordinator._stat_buckets.items()
            },
        },
        "sensor_data": sensor_data,
    }
