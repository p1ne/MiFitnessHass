"""Coordinator / sensor / diagnostics tests with the data API mocked."""
import json
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mi_fitness import coordinator as coord_mod
from custom_components.mi_fitness.api import FetchResult
from custom_components.mi_fitness.const import DEFAULT_START_WATERMARK, DOMAIN
from custom_components.mi_fitness.coordinator import MiFitnessCoordinator
from custom_components.mi_fitness.diagnostics import async_get_config_entry_diagnostics
from custom_components.mi_fitness.sensor import SENSOR_TYPES, MiFitnessSensor

DATA = {"auth_method": "tokens", "user_id": "123", "c_user_id": "cu", "ssecurity": "SECRET_S",
        "service_token": "SECRET_T", "pass_token": "SECRET_P", "region": "de",
        "api_device_suffix": "a-b"}


def it(key, ts, **v):
    return {"key": key, "time": ts, "value": json.dumps(v)}


async def make(hass, results, stored=None):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="123", data=DATA)
    entry.add_to_hass(hass)
    client = MagicMock()
    client.fetch_all_since = MagicMock(side_effect=results)
    c = MiFitnessCoordinator(hass, client, entry)
    c._store.async_load = AsyncMock(return_value=stored)
    c._store.async_save = AsyncMock()
    c._import_statistics = MagicMock()
    c._import_step_statistics = AsyncMock()
    await c.async_load_stored_state()
    return entry, c


async def test_probe_falls_back_when_estimate_skips_history(hass):
    now = time.time()
    entry, c = await make(hass, [FetchResult(items=[it("heart_rate", int(now) - 3600, bpm=60)],
                                             watermark=999)])
    assert c._watermark > DEFAULT_START_WATERMARK          # extrapolated start used
    await c.async_refresh()
    assert c._watermark == DEFAULT_START_WATERMARK          # only recent data → fell back
    assert c.update_interval == timedelta(seconds=30)


async def test_probe_accepted_when_old_data_present(hass):
    now = time.time()
    entry, c = await make(hass, [FetchResult(items=[it("heart_rate", int(now) - 28 * 86400, bpm=60)],
                                             watermark=12345)])
    await c.async_refresh()
    assert c._watermark == 12345


async def test_has_more_keeps_catching_up(hass):
    now = int(time.time())
    stored = {"watermark": 10, "day_wm": 10, "day_num": dt_util.now().toordinal()}
    entry, c = await make(hass, [FetchResult(items=[it("heart_rate", now - 60, bpm=70)],
                                             watermark=20, has_more=True)], stored)
    c._initial_load_done = True
    await c.async_refresh()
    assert c.update_interval == timedelta(seconds=30)


async def test_protocol_errors_lead_to_reauth(hass):
    stored = {"watermark": 10, "day_wm": 10, "day_num": dt_util.now().toordinal()}
    bad = lambda: FetchResult(watermark=10, errored=True, protocol_error=True)
    entry, c = await make(hass, [bad(), bad(), bad()], stored)
    with patch.object(c, "_async_silent_refresh", AsyncMock(return_value=False)) as sr:
        await c.async_refresh(); await c.async_refresh()
        assert not any(f["context"]["source"] == SOURCE_REAUTH
                       for f in hass.config_entries.flow.async_progress())
        await c.async_refresh()
        await hass.async_block_till_done()
    sr.assert_awaited_once()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress())


async def test_network_errors_do_not_trigger_reauth(hass):
    stored = {"watermark": 10, "day_wm": 10, "day_num": dt_util.now().toordinal()}
    entry, c = await make(hass, [FetchResult(watermark=10, errored=True)] * 5, stored)
    with patch.object(c, "_async_silent_refresh", AsyncMock(return_value=False)) as sr:
        for _ in range(5):
            await c.async_refresh()
    sr.assert_not_awaited()


async def test_stale_heart_rate_is_unknown_and_diagnostics_redacted(hass):
    now = int(time.time())
    stored = {"watermark": 10, "day_wm": 10, "day_num": dt_util.now().toordinal()}
    entry, c = await make(hass, [FetchResult(items=[it("heart_rate", now - 3 * 3600, bpm=70),
                                                    it("stress", now - 3600, stress=0)],
                                             watermark=20)], stored)
    await c.async_refresh()
    desc = {d.key: d for d in SENSOR_TYPES}
    hr = MiFitnessSensor(c, desc["heart_rate"], "123", entry.entry_id)
    st = MiFitnessSensor(c, desc["stress"], "123", entry.entry_id)
    assert c.data["heart_rate"] == 70 and hr.native_value is None      # 3 h old → unknown
    assert st.native_value == 0                                       # 1 h old, 0 is valid
    entry.runtime_data = c
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(diag, default=str)
    assert "SECRET" not in dump and "123" not in json.dumps(diag["entry"])


async def test_translations_keep_english_entity_ids(hass):
    en = json.load(open("custom_components/mi_fitness/translations/en.json"))["entity"]["sensor"]
    ru = json.load(open("custom_components/mi_fitness/translations/ru.json"))["entity"]["sensor"]
    keys = {d.translation_key for d in SENSOR_TYPES}
    assert keys == set(en) == set(ru)
    assert en["steps_today"]["name"] == "Steps Today"
