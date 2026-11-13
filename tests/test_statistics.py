"""Step statistics continue the sum across a long gap."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from homeassistant.components.recorder.statistics import async_add_external_statistics
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.mi_fitness.const import DOMAIN
from custom_components.mi_fitness.coordinator import MiFitnessCoordinator


async def test_sum_continues_after_60_day_gap(recorder_mock, hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="123", data={"user_id": "123"})
    entry.add_to_hass(hass)
    c = MiFitnessCoordinator(hass, MagicMock(), entry)
    meta = c._statistic_metadata("steps", "Steps", "steps", has_sum=True)
    old = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=60)
    async_add_external_statistics(hass, meta, [{"start": old, "state": 500, "sum": 12345}])
    await async_wait_recording_done(hass)
    total = await c._async_last_step_sum_before(meta["statistic_id"], old + timedelta(days=60))
    assert total == 12345
