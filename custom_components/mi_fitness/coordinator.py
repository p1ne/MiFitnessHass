"""DataUpdateCoordinator for Mi Fitness."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import FetchResult, MiFitnessApiError, MiFitnessAuthError, MiFitnessClient
from .auth import try_silent_token_refresh
from .const import (
    CONF_LOGIN_DEVICE_ID,
    CONF_PASS_TOKEN,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    DEFAULT_START_WATERMARK,
    DEFAULT_START_WATERMARK_TS,
    DOMAIN,
    KEY_CALORIES,
    KEY_HEART_RATE,
    KEY_RESTING_HEART_RATE,
    KEY_SLEEP,
    KEY_SPO2,
    KEY_STEPS,
    KEY_STRESS,
    KEY_VITALITY,
    KEY_VALID_STAND,
    KEY_WEIGHT,
    SCAN_INTERVAL_MINUTES,
    STORAGE_KEY,
    STORAGE_VERSION,
    WATERMARK_PER_DAY,
)

try:
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # HA < 2025.4 — metadata uses has_mean instead
    StatisticMeanType = None

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=SCAN_INTERVAL_MINUTES)

# Readings written to hourly long-term statistics mi_fitness:<metric>_<user_id>
# as mean/min/max: metric → (name, unit)
_STATISTICS = {
    "heart_rate": ("Heart Rate", "bpm"),
    "spo2":       ("Blood Oxygen (SpO2)", "%"),
}

# Sleep stage state codes from the API
_SLEEP_STATE_NAMES = {2: "light", 3: "deep", 4: "rem", 5: "awake"}

# Sleep sessions shorter than this are treated as naps: a nap does not replace
# a main sleep that ended recently in the "current sleep" sensors, and naps are
# not used for the per-day history / chronotype when a longer sleep exists.
_NAP_MAX_MINUTES = 180
_NAP_WINDOW_SECONDS = 20 * 3600

_SLEEP_KEEP_DAYS = 30

# Consecutive polls with only protocol errors before credentials are renewed
_PROTOCOL_ERRORS_FOR_REAUTH = 3

# First install: how far back data must reach for the estimated start
# watermark to be accepted (sleep is kept 30 days)
_PROBE_MIN_AGE_DAYS = 25
_START_MARGIN_DAYS = 35


def _estimated_start_watermark() -> int:
    """Watermark ~_START_MARGIN_DAYS ago, extrapolated from the anchor.

    Deliberately conservative (80 % of the observed growth rate): starting
    too early only costs a longer catch-up, starting too late would skip data.
    """
    days = (time.time() - DEFAULT_START_WATERMARK_TS) / 86400 - _START_MARGIN_DAYS
    if days <= 0:
        return DEFAULT_START_WATERMARK
    return DEFAULT_START_WATERMARK + int(days * WATERMARK_PER_DAY * 0.8)


# ── Time helpers (always in the HA-configured time zone, not the OS one) ─────

def _local(ts: float) -> datetime:
    """Aware datetime in Home Assistant's configured time zone."""
    return dt_util.as_local(dt_util.utc_from_timestamp(ts))


def _date_str(ts: float) -> str:
    return _local(ts).strftime("%Y-%m-%d")


def _day_start_ts() -> int:
    """Unix timestamp of 00:00 local (HA time zone) today."""
    return int(dt_util.start_of_local_day().timestamp())


def _local_day_num() -> int:
    """Calendar day number in HA local time (daily sensors reset at local midnight)."""
    return dt_util.now().toordinal()


def _today_str() -> str:
    return dt_util.now().strftime("%Y-%m-%d")


def _days_ago_str(days: int) -> str:
    return (dt_util.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _parse_value(item: dict) -> Any:
    """Parse 'value' field from a data_list item (JSON string or raw)."""
    raw = item.get("value", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _sleep_quality_score(
    duration: int,
    deep: int,
    rem: int,
    awake_count: int,
) -> int:
    """
    Calculate sleep quality score 0-100.

    Weights:
      30 % — duration proximity to 8h optimum
      35 % — deep sleep ratio (target ≥20 %)
      20 % — REM ratio (target ≥20 %)
      15 % — continuity (awake interruptions penalty)
    """
    if not duration or duration < 60:
        return 0

    dur_score = max(0, 100 - abs(duration - 480) // 5)
    deep_ratio = deep / duration if duration else 0
    deep_score = min(100, int(deep_ratio * 500))
    rem_ratio = rem / duration if duration else 0
    rem_score = min(100, int(rem_ratio * 500))
    continuity = max(40, 100 - (awake_count or 0) * 8)

    total = (
        dur_score  * 0.30
        + deep_score * 0.35
        + rem_score  * 0.20
        + continuity * 0.15
    )
    return round(total)


def _sleep_chronotype_6(history: list[dict]) -> str:
    """
    Classify into one of Xiaomi's 6 sleep animals using multi-night history.

      sheep      — early bed, sleeps VERY LITTLE (short duration, gets tired)
      penguin    — early bed, sleeps ok but EASILY AWAKENED (high awake count)
      brown_bear — healthy/normal schedule (22-23h), plenty of restful sleep
      koala      — stays up late, sleeps SOUNDLY once asleep (low awake, good deep)
      night_owl  — stays up late, sleeps RESTLESSLY (high awake count)
      shark      — very late and/or sleeps LITTLE (severely deprived)
    """
    records = [r for r in history if r.get("bedtime") and r.get("duration", 0) >= 120]
    if not records:
        return "brown_bear"

    bed_hours = []
    for r in records:
        bed_dt = _local(r["bedtime"])
        h = bed_dt.hour + bed_dt.minute / 60
        if h < 7:            # normalise midnight crossings: 00:30 → 24.5
            h += 24
        bed_hours.append(h)

    avg_bed      = sum(bed_hours) / len(bed_hours)
    avg_awake    = sum(r.get("awake_count", 0) for r in records) / len(records)
    avg_duration = sum(r.get("duration", 0) for r in records) / len(records)

    peaceful   = avg_awake < 1.5
    very_short = avg_duration < 300

    if avg_bed >= 25.5 or very_short:
        return "shark"
    if avg_bed >= 23.0:
        return "koala" if peaceful else "night_owl"
    if avg_bed >= 22.0:
        return "brown_bear" if peaceful else "night_owl"
    return "sheep" if peaceful else "penguin"


def _stages_to_list(items: list[dict]) -> list[dict]:
    """Convert raw API sleep items into clean stage dicts {start, end, state, label}."""
    out = []
    for seg in items:
        state = seg.get("state", 0)
        out.append({
            "start": seg.get("start_time", 0),
            "end":   seg.get("end_time",   0),
            "state": state,
            "label": _SLEEP_STATE_NAMES.get(state, "unknown"),
        })
    return out


def _overlaps(a: dict, b: dict) -> bool:
    return a["bedtime"] < b["wakeup"] and b["bedtime"] < a["wakeup"]


def _contains(outer: dict, inner: dict) -> bool:
    return outer["bedtime"] <= inner["bedtime"] and inner["wakeup"] <= outer["wakeup"]


class MiFitnessCoordinator(DataUpdateCoordinator):
    """Polls Mi Fitness API and aggregates sensor data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: MiFitnessClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,   # needed for ConfigEntryAuthFailed → re-auth
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self._client = client
        self._entry = entry
        self._entry_id = entry.entry_id
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
        self._watermark: int = 0
        self._day_wm: int = 0       # watermark saved at start of each new day
        self._day_num: int = 0      # calendar day when _day_wm was recorded
        self._sensor_data: dict[str, Any] = {}
        # Per-day "main sleep" (derived from _sleep_sessions, keyed by wake-up date)
        self._sleep_history: dict[str, dict] = {}
        # Every sleep session (night + naps) keyed by str(bedtime), 30 days
        self._sleep_sessions: dict[str, dict] = {}
        self._steps_history: dict[str, dict] = {}
        # Per-day step records keyed by timestamp: {date_str: {ts: {...}}}.
        # The server re-sends the same interval with a new watermark as data
        # accumulates, so we DEDUPE by ts (replace) instead of summing.
        self._step_buckets: dict[str, dict] = {}
        # Same dedupe for calorie records: {date_str: {ts: kcal}}
        self._calorie_buckets: dict[str, dict] = {}
        # Readings per metric and UTC hour: {metric: {hour_start_ts: {ts: value}}},
        # deduped by ts like steps. Feeds hourly statistics and today's HR avg/min/max.
        self._stat_buckets: dict[str, dict] = {}
        # Days whose step statistics must be rewritten after the next merge
        self._pending_step_dates: set[str] = set()
        self._initial_load_done: bool = False
        self._fetch_errors: int = 0   # consecutive failed polls → backoff
        self._protocol_errors: int = 0
        self._probing_start: bool = False

    async def async_load_stored_state(self):
        """Load persisted state. Called once before the first refresh.

        Deliberately NOT named ``_async_setup`` — that is a DataUpdateCoordinator
        hook (HA 2024.8+) auto-invoked by ``async_config_entry_first_refresh``,
        so reusing the name would run this twice.
        """
        stored = await self._store.async_load()
        if stored:
            self._watermark     = stored.get("watermark", 0)
            self._day_wm        = stored.get("day_wm", 0)
            self._day_num       = stored.get("day_num", 0)
            # Migrate per-day history of older versions into sessions
            self._sleep_sessions = {
                str(r["bedtime"]): r
                for r in stored.get("sleep_history", {}).values()
                if r.get("bedtime") and r.get("wakeup")
            }
            self._sleep_sessions.update(stored.get("sleep_sessions", {}))
            self._rebuild_sleep_history()
            self._steps_history   = stored.get("steps_history", {})
            self._step_buckets    = stored.get("step_buckets", {})
            self._calorie_buckets = stored.get("calorie_buckets", {})
            # (re)write step statistics for all stored days on startup — also
            # backfills installs that had step data before statistics existed
            self._pending_step_dates = set(self._step_buckets)
            self._stat_buckets  = stored.get("stat_buckets", {})
            self._sensor_data   = stored.get("sensor_data", {})
            self._sensor_data["sleep_history_14d"] = self._build_sleep_history_list()
            self._sensor_data["sleep_sessions_30d"] = sorted(
                self._sleep_sessions.values(), key=lambda r: r["bedtime"], reverse=True
            )
            # On restart roll back to start-of-today so we re-fetch today's data.
            # Records are deduped by timestamp, so re-fetching is idempotent.
            if self._day_wm:
                self._watermark = self._day_wm
            elif self._watermark == 0:
                self._set_initial_watermark()
        else:
            self._set_initial_watermark()

    def _set_initial_watermark(self) -> None:
        """First install: start roughly a month back instead of at the old anchor."""
        estimate = _estimated_start_watermark()
        self._watermark = estimate
        self._probing_start = estimate > DEFAULT_START_WATERMARK

    async def _async_save(self) -> None:
        await self._store.async_save({
            "watermark":       self._watermark,
            "day_wm":          self._day_wm,
            "day_num":         self._day_num,
            "sleep_sessions":  self._sleep_sessions,
            "steps_history":   self._steps_history,
            "step_buckets":    self._step_buckets,
            "calorie_buckets": self._calorie_buckets,
            "stat_buckets":    self._stat_buckets,
            "sensor_data":     self._sensor_data,
        })

    @property
    def client(self) -> MiFitnessClient:
        return self._client

    async def _async_fetch(self, max_pages: int) -> FetchResult:
        return await self.hass.async_add_executor_job(
            self._client.fetch_all_since, self._watermark, max_pages,
        )

    async def _async_silent_refresh(self) -> bool:
        """Get a fresh serviceToken with the stored passToken (no 2FA). True on success."""
        data = self._entry.data
        pass_token = data.get(CONF_PASS_TOKEN, "")
        user_id    = data.get(CONF_USER_ID, "")
        if not (pass_token and user_id):
            return False
        _LOGGER.info("Mi Fitness: refreshing serviceToken with passToken")
        creds = await self.hass.async_add_executor_job(
            try_silent_token_refresh, pass_token, user_id,
            data.get(CONF_LOGIN_DEVICE_ID, ""),
        )
        if not creds.get("service_token"):
            _LOGGER.warning("Silent token refresh failed — passToken may have expired")
            return False

        # Persist everything that changed, including a rotated passToken:
        # Xiaomi rotates it and eventually invalidates the old one.
        new_data = dict(data)
        for key, value in (
            (CONF_PASS_TOKEN,    creds.get("pass_token")),
            (CONF_SERVICE_TOKEN, creds.get("service_token")),
            (CONF_SSECURITY,     creds.get("ssecurity")),
        ):
            if value and value != data.get(key):
                new_data[key] = value
        if creds.get("ssecurity"):
            self._client.ssecurity = creds["ssecurity"]
        self._client.update_service_token(creds["service_token"])
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        _LOGGER.info("Silent token refresh succeeded")
        return True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch new data from API and merge into sensor state."""

        # Reset daily metrics when calendar day changes.
        # Also on the first poll after a restart: HA may have been down over midnight.
        today_day = _local_day_num()
        if today_day != self._day_num and self._day_num != 0:
            _LOGGER.debug("Mi Fitness: new day — clearing daily sensor accumulators")
            for _k in (
                "steps_today", "distance_today", "step_calories_today", "steps_hourly",
                "vitality_high", "vitality_medium", "vitality_low", "vitality_ts",
                "calories_today",
                "heart_rate_avg_today", "heart_rate_max_today", "heart_rate_min_today",
                "stand_hours_today", "stand_hours_list",
            ):
                self._sensor_data.pop(_k, None)

        # First refresh: fetch only 1 page to verify connectivity fast
        # (avoids CancelledError timeout during async_config_entry_first_refresh)
        max_pages = 1 if not self._initial_load_done else 30
        self._initial_load_done = True

        try:
            try:
                res = await self._async_fetch(max_pages)
            except MiFitnessAuthError as exc:
                if not await self._async_silent_refresh():
                    raise ConfigEntryAuthFailed(f"Mi Fitness auth error: {exc}") from exc
                # Retry right away instead of leaving sensors stale for a full interval
                try:
                    res = await self._async_fetch(max_pages)
                except MiFitnessAuthError as exc2:
                    raise ConfigEntryAuthFailed(
                        f"Mi Fitness auth error after token refresh: {exc2}"
                    ) from exc2
        except MiFitnessApiError as exc:
            raise UpdateFailed(str(exc)) from exc

        items = res.items

        # First install: the extrapolated start watermark may be past the data
        # we want (or past all data). Then fall back to the known anchor.
        if self._probing_start and not res.errored:
            self._probing_start = False
            oldest = min(
                ((i.get("time") or i.get("update_time") or 0) for i in items), default=0
            )
            if not items or oldest > time.time() - _PROBE_MIN_AGE_DAYS * 86400:
                _LOGGER.info(
                    "Mi Fitness: estimated start point skipped history — "
                    "re-scanning from the built-in anchor"
                )
                self._watermark = DEFAULT_START_WATERMARK
                self.update_interval = timedelta(seconds=30)
                return dict(self._sensor_data)

        if items:
            self._merge_items(items)
            if self._pending_step_dates:
                await self._import_step_statistics()

        # Undecryptable answers / API error codes, again and again, mean the
        # stored ssecurity no longer matches: refresh, otherwise ask to re-auth.
        if res.errored and res.protocol_error and not items:
            self._protocol_errors += 1
            if self._protocol_errors >= _PROTOCOL_ERRORS_FOR_REAUTH:
                self._protocol_errors = 0
                if not await self._async_silent_refresh():
                    raise ConfigEntryAuthFailed(
                        "Mi Fitness API answers can no longer be decrypted — "
                        "credentials need to be renewed"
                    )
        elif not res.errored or items:
            self._protocol_errors = 0

        # Dynamic interval: catching up → 30 s, up to date → 15 min,
        # repeated failures → exponential backoff (don't hammer a rate-limit).
        if items:
            self._fetch_errors = 0
            latest_ts = max(
                (item.get("time") or item.get("update_time") or 0) for item in items
            )
            # has_more: the page limit was hit and the server still has data
            catching_up = res.has_more or latest_ts < (time.time() - 2 * 86400)
            self.update_interval = timedelta(seconds=30) if catching_up else SCAN_INTERVAL
            if catching_up:
                _LOGGER.debug(
                    "Mi Fitness: catching up — latest_item=%s has_more=%s",
                    _date_str(latest_ts), res.has_more,
                )
        elif res.errored:
            self._fetch_errors += 1
            backoff = min(30 * (2 ** self._fetch_errors), 600)
            self.update_interval = timedelta(seconds=backoff)
            _LOGGER.warning(
                "Mi Fitness: fetch failed x%d — backing off to %ds",
                self._fetch_errors, backoff,
            )
        else:
            self._fetch_errors = 0
            if self.update_interval != SCAN_INTERVAL:
                self.update_interval = SCAN_INTERVAL
                _LOGGER.debug(
                    "Mi Fitness: caught up, switching to %s min interval",
                    SCAN_INTERVAL_MINUTES,
                )

        # Persist only after merging, so the stored state matches the stored
        # watermark (saving before the merge lost the last poll on restart).
        new_wm = res.watermark
        if new_wm and new_wm != self._watermark:
            self._watermark = new_wm
            if today_day != self._day_num:
                # New day — snapshot watermark so restart can roll back to here
                self._day_wm  = new_wm
                self._day_num = today_day
            await self._async_save()
        elif items:
            await self._async_save()

        return dict(self._sensor_data)

    def _merge_items(self, items: list[dict]) -> None:
        """Merge new data_list items into aggregated sensor state."""
        now       = time.time()
        today     = _day_start_ts()
        tomorrow  = today + 86400

        # "recent" sensors (HR, steps, vitality): 14 days
        # sleep: 30 days, "infrequent" sensors (weight, SpO2): 90 days
        recent_cutoff = now - 14 * 86400
        sleep_cutoff  = now - _SLEEP_KEEP_DAYS * 86400
        slow_cutoff   = now - 90 * 86400

        touched_step_dates: set[str] = set()
        touched_calorie_dates: set[str] = set()
        touched_hours: dict[str, set[str]] = {}
        stand_hours_today_set: set[int] = set()
        sleep_touched = False

        for item in items:
            key = item.get("key", "")
            ts  = item.get("time") or item.get("update_time") or 0
            val = _parse_value(item)

            # ── Steps ─────────────────────────────────────────────────────
            if key == KEY_STEPS:
                if ts >= recent_cutoff and isinstance(val, dict):
                    local = _local(ts)
                    date_str = local.strftime("%Y-%m-%d")
                    day = self._step_buckets.setdefault(date_str, {})
                    day[str(ts)] = {
                        "steps":    val.get("steps", 0),
                        "distance": val.get("distance", 0),
                        "calories": val.get("calories", 0),
                        "hour":     local.hour,
                    }
                    touched_step_dates.add(date_str)

            # ── Heart rate ────────────────────────────────────────────────
            elif key == KEY_HEART_RATE:
                if ts < recent_cutoff:
                    continue
                prev = self._sensor_data.get("heart_rate_ts", 0)
                if ts > prev and isinstance(val, dict) and val.get("bpm"):
                    self._sensor_data["heart_rate"]      = val["bpm"]
                    self._sensor_data["heart_rate_ts"]   = ts
                    self._sensor_data["heart_rate_type"] = val.get("type", 0)
                if isinstance(val, dict) and val.get("bpm"):
                    self._add_reading("heart_rate", ts, val["bpm"], touched_hours)

            # ── Resting heart rate ────────────────────────────────────────
            elif key == KEY_RESTING_HEART_RATE:
                if ts < recent_cutoff:
                    continue
                prev = self._sensor_data.get("resting_hr_ts", 0)
                if ts > prev and isinstance(val, dict) and val.get("bpm"):
                    self._sensor_data["resting_heart_rate"] = val["bpm"]
                    self._sensor_data["resting_hr_ts"]      = ts

            # ── Sleep ─────────────────────────────────────────────────────
            elif key == KEY_SLEEP:
                if ts >= sleep_cutoff:
                    sleep_touched |= self._process_sleep(val, ts)

            # ── Calories ──────────────────────────────────────────────────
            # Records are re-sent like steps → dedupe by ts, never sum a batch.
            elif key == KEY_CALORIES:
                if ts >= today - 86400 and isinstance(val, dict):
                    date_str = _date_str(ts)
                    self._calorie_buckets.setdefault(date_str, {})[str(ts)] = (
                        val.get("calories", 0) or 0
                    )
                    touched_calorie_dates.add(date_str)

            # ── Weight ────────────────────────────────────────────────────
            elif key == KEY_WEIGHT:
                if ts < slow_cutoff:
                    continue
                prev = self._sensor_data.get("weight_ts", 0)
                if ts > prev and isinstance(val, dict) and val.get("weight"):
                    self._sensor_data["weight"]    = val["weight"]
                    self._sensor_data["weight_ts"] = ts

            # ── SpO2 ──────────────────────────────────────────────────────
            elif key == KEY_SPO2:
                if ts < slow_cutoff:
                    continue
                prev = self._sensor_data.get("spo2_ts", 0)
                if ts > prev and isinstance(val, dict):
                    spo2_val = val.get("spo2") or val.get("value") or val.get("blood_oxygen")
                    if spo2_val:
                        self._sensor_data["spo2"]    = spo2_val
                        self._sensor_data["spo2_ts"] = ts
                if ts >= recent_cutoff and isinstance(val, dict) and val.get("spo2"):
                    self._add_reading("spo2", ts, val["spo2"], touched_hours)

            # ── Stress ────────────────────────────────────────────────────
            elif key == KEY_STRESS:
                if ts < recent_cutoff:
                    continue
                if not isinstance(val, dict) or val.get("stress") is None:
                    continue
                # 0 is a valid reading; prefer the measurement's own timestamp
                stress_ts = val.get("time") or ts
                if stress_ts > self._sensor_data.get("stress_ts", 0):
                    self._sensor_data["stress"]    = val["stress"]
                    self._sensor_data["stress_ts"] = stress_ts

            # ── Vitality ──────────────────────────────────────────────────
            elif key == KEY_VITALITY:
                if ts < recent_cutoff:
                    continue
                prev_day = self._sensor_data.get("vitality_ts", 0)
                if ts > prev_day and isinstance(val, dict):
                    self._sensor_data["vitality_high"]   = val.get("daily_high_intensity_vitality", 0)
                    self._sensor_data["vitality_medium"] = val.get("daily_medium_intensity_vitality", 0)
                    self._sensor_data["vitality_low"]    = val.get("daily_low_intensity_vitality", 0)
                    self._sensor_data["vitality_ts"]     = ts

            # ── Valid stand ───────────────────────────────────────────────
            elif key == KEY_VALID_STAND:
                if today <= ts < tomorrow:
                    stand_hours_today_set.add(_local(ts).hour)

        today_str = _date_str(today)

        # Steps: rebuild day totals from deduped buckets (sum of unique-ts values)
        if touched_step_dates:
            self._pending_step_dates |= touched_step_dates
            cutoff_str = _days_ago_str(14)
            self._step_buckets = {
                d: b for d, b in self._step_buckets.items() if d >= cutoff_str
            }
            self._steps_history = {}
            for date_str, buckets in self._step_buckets.items():
                self._steps_history[date_str] = {
                    "date":     date_str,
                    "steps":    sum(b["steps"]    for b in buckets.values()),
                    "distance": sum(b["distance"] for b in buckets.values()),
                    "calories": sum(b["calories"] for b in buckets.values()),
                }
            tb = self._step_buckets.get(today_str)
            if tb:
                self._sensor_data["steps_today"]         = sum(b["steps"]    for b in tb.values())
                self._sensor_data["distance_today"]      = sum(b["distance"] for b in tb.values())
                self._sensor_data["step_calories_today"] = sum(b["calories"] for b in tb.values())
                hourly: dict[int, dict] = {}
                for b in tb.values():
                    he = hourly.setdefault(b["hour"], {"steps": 0, "distance": 0})
                    he["steps"]    += b["steps"]
                    he["distance"] += b["distance"]
                self._sensor_data["steps_hourly"] = [
                    {"hour": h, "steps": v["steps"], "distance": v["distance"]}
                    for h, v in sorted(hourly.items())
                ]
            self._sensor_data["steps_history_14d"] = sorted(
                self._steps_history.values(), key=lambda x: x["date"], reverse=True
            )[:14]

        if touched_calorie_dates:
            yesterday_str = _days_ago_str(1)
            self._calorie_buckets = {
                d: b for d, b in self._calorie_buckets.items() if d >= yesterday_str
            }
            cb = self._calorie_buckets.get(today_str)
            if cb:
                self._sensor_data["calories_today"] = sum(cb.values())

        for metric, hours in touched_hours.items():
            self._import_statistics(metric, hours)
            # keep 2 days: enough for today's values and late re-sends
            self._stat_buckets[metric] = {
                h: b for h, b in self._stat_buckets[metric].items() if int(h) >= today - 86400
            }

        if "heart_rate" in touched_hours:
            bpms = [
                bpm
                for h, b in self._stat_buckets["heart_rate"].items()
                if today <= int(h) < tomorrow
                for bpm in b.values()
            ]
            if bpms:
                self._sensor_data["heart_rate_avg_today"] = round(sum(bpms) / len(bpms))
                self._sensor_data["heart_rate_max_today"] = max(bpms)
                self._sensor_data["heart_rate_min_today"] = min(bpms)

        if stand_hours_today_set:
            prev_set = set(self._sensor_data.get("stand_hours_list", []))
            merged   = sorted(prev_set | stand_hours_today_set)
            self._sensor_data["stand_hours_list"]  = merged
            self._sensor_data["stand_hours_today"] = len(merged)

        if sleep_touched or "sleep_history_14d" not in self._sensor_data:
            self._rebuild_sleep_history()
            history_list = self._build_sleep_history_list()
            self._sensor_data["sleep_history_14d"] = history_list
            self._sensor_data["sleep_sessions_30d"] = sorted(
                self._sleep_sessions.values(), key=lambda r: r["bedtime"], reverse=True
            )
            if history_list:
                self._sensor_data["sleep_chronotype"] = _sleep_chronotype_6(history_list)

    # ── Statistics ────────────────────────────────────────────────────────────

    def _add_reading(
        self, metric: str, ts: int, value: float, touched: dict[str, set[str]]
    ) -> None:
        """Store a reading in its hour bucket (replacing re-sends of the same ts)."""
        hour = str(ts - ts % 3600)
        self._stat_buckets.setdefault(metric, {}).setdefault(hour, {})[str(ts)] = value
        touched.setdefault(metric, set()).add(hour)

    def _import_statistics(self, metric: str, hours: set[str]) -> None:
        """Write hourly mean/min/max of a metric as external long-term statistics."""
        name, unit = _STATISTICS[metric]
        metadata = self._statistic_metadata(metric, name, unit, has_sum=False)
        statistics: list[StatisticData] = []
        for hour in sorted(hours, key=int):
            values = list(self._stat_buckets[metric][hour].values())
            statistics.append(StatisticData(
                start=datetime.fromtimestamp(int(hour), tz=timezone.utc),
                mean=sum(values) / len(values),
                min=min(values),
                max=max(values),
            ))
        async_add_external_statistics(self.hass, metadata, statistics)

    async def _import_step_statistics(self) -> None:
        """Write hourly steps as the cumulative statistic mi_fitness:steps_<user_id>.

        Rewrites everything from the earliest touched day on, continuing
        the sum from the last row before it.
        """
        first_day = min(self._pending_step_dates)
        self._pending_step_dates = set()
        hours: dict[int, int] = {}
        for date_str, buckets in self._step_buckets.items():
            if date_str < first_day:
                continue
            for ts, b in buckets.items():
                hour = int(ts) - int(ts) % 3600
                hours[hour] = hours.get(hour, 0) + b["steps"]
        if not hours:
            return

        metadata = self._statistic_metadata("steps", "Steps", "steps", has_sum=True)
        start = datetime.fromtimestamp(min(hours), tz=timezone.utc)
        total = await self._async_last_step_sum_before(metadata["statistic_id"], start)

        statistics: list[StatisticData] = []
        for hour in sorted(hours):
            total += hours[hour]
            statistics.append(StatisticData(
                start=datetime.fromtimestamp(hour, tz=timezone.utc),
                state=hours[hour],
                sum=total,
            ))
        async_add_external_statistics(self.hass, metadata, statistics)

    async def _async_last_step_sum_before(self, statistic_id: str, start: datetime) -> float:
        """Cumulative sum of the last row before ``start``.

        Looks back 30 days first (cheap); if HA had no step rows in that window
        (e.g. the integration was disabled for a while), looks further back so
        the sum continues instead of restarting at 0 — a restart at 0 shows up
        as a huge negative "change" in the statistics graphs.
        """
        recorder = get_instance(self.hass)
        for days in (30, 3650):
            before = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass, start - timedelta(days=days), start,
                {statistic_id}, "hour", None, {"sum"},
            )
            rows = before.get(statistic_id)
            if rows:
                return rows[-1].get("sum") or 0
        return 0

    def _statistic_metadata(
        self, metric: str, name: str, unit: str, has_sum: bool
    ) -> StatisticMetaData:
        user_id = self._entry.data.get(CONF_USER_ID, "")
        metadata: StatisticMetaData = {
            "has_sum":             has_sum,
            "name":                f"Mi Fitness ({user_id}) {name}",
            "source":              DOMAIN,
            "statistic_id":        f"{DOMAIN}:{metric}_{user_id}",
            "unit_of_measurement": unit,
        }
        if StatisticMeanType is not None:
            metadata["mean_type"] = (
                StatisticMeanType.NONE if has_sum else StatisticMeanType.ARITHMETIC
            )
            metadata["unit_class"] = None
        else:
            metadata["has_mean"] = not has_sum
        return metadata

    # ── Sleep processing ──────────────────────────────────────────────────────

    def _process_sleep(self, val: Any, ts: int) -> bool:
        """Store a sleep session and update the "current sleep" sensors.

        The server keeps re-sending a night while it grows: same bedtime with a
        later wake-up, or a merged record with an earlier bedtime that replaces
        fragments. Sessions are keyed by bedtime; overlapping fragments that a
        new record covers are dropped. Returns True if anything changed.
        """
        if not isinstance(val, dict):
            return False
        bedtime  = val.get("bedtime", 0)
        wakeup   = val.get("wake_up_time", 0)
        duration = val.get("duration", 0)
        if not bedtime or not wakeup or wakeup <= bedtime or duration <= 0:
            return False

        deep        = val.get("sleep_deep_duration", 0)
        light       = val.get("sleep_light_duration", 0)
        rem         = val.get("sleep_rem_duration", val.get("rem_duration", 0))
        awake_count = val.get("awake_count", 0)
        quality     = _sleep_quality_score(duration, deep, rem, awake_count)
        time_in_bed = (wakeup - bedtime) / 60
        efficiency  = round(duration / time_in_bed * 100) if time_in_bed > 0 else 0

        record = {
            "bedtime": bedtime, "wakeup": wakeup, "duration": duration,
            "deep": deep, "light": light, "rem": rem,
            "awake_count": awake_count, "quality": quality,
            "efficiency": efficiency, "updated_at": ts,
            "is_uncomplete": bool(val.get("is_uncomplete", False)),
            "in_bed_at": val.get("bed_timestamp"),
            "out_of_bed_at": val.get("out_bed_timestamp"),
        }

        session_key = str(bedtime)
        previous = self._sleep_sessions.get(session_key)
        if previous and ts < previous.get("updated_at", 0):
            return False

        # Resolve overlaps with other sessions (fragments vs merged night)
        for key, other in list(self._sleep_sessions.items()):
            if key == session_key or not other.get("wakeup") or not _overlaps(record, other):
                continue
            if _contains(record, other):
                del self._sleep_sessions[key]          # new record covers the fragment
            elif _contains(other, record):
                return False                           # stale fragment of a known night
            elif ts >= other.get("updated_at", 0):
                del self._sleep_sessions[key]
            else:
                return False

        self._sleep_sessions[session_key] = record
        cutoff_ts = time.time() - _SLEEP_KEEP_DAYS * 86400
        self._sleep_sessions = {
            k: r for k, r in self._sleep_sessions.items() if r.get("wakeup", 0) >= cutoff_ts
        }

        if self._should_become_current(record, ts):
            self._sensor_data.update({
                "sleep_bedtime":     bedtime,
                "sleep_wakeup":      wakeup,
                "sleep_duration":    duration,
                "sleep_deep":        deep,
                "sleep_light":       light,
                "sleep_rem":         rem,
                "sleep_avg_hr":      val.get("avg_hr", 0),
                "sleep_max_hr":      val.get("max_hr", 0),
                "sleep_min_hr":      val.get("min_hr", 0),
                "sleep_awake_count": awake_count,
                "sleep_ts":          ts,
                "sleep_quality":     quality,
                "sleep_efficiency":  efficiency,
                "sleep_stages":      _stages_to_list(val.get("items") or []),
            })
        return True

    def _should_become_current(self, record: dict, ts: int) -> bool:
        cur_bed  = self._sensor_data.get("sleep_bedtime", 0)
        cur_wake = self._sensor_data.get("sleep_wakeup", 0)
        cur_dur  = self._sensor_data.get("sleep_duration", 0)
        if not cur_bed or not cur_wake:
            return True
        current = {"bedtime": cur_bed, "wakeup": cur_wake}
        if record["bedtime"] == cur_bed:
            return ts >= self._sensor_data.get("sleep_ts", 0)
        if _overlaps(record, current):
            # Merged/extended version of the current night
            return True
        if record["bedtime"] < cur_bed:
            return False
        # A later, separate session: a nap must not hide last night's sleep
        is_nap = record["duration"] < _NAP_MAX_MINUTES
        main_is_recent = (
            cur_dur >= _NAP_MAX_MINUTES and record["bedtime"] - cur_wake < _NAP_WINDOW_SECONDS
        )
        return not (is_nap and main_is_recent)

    def _rebuild_sleep_history(self) -> None:
        """Per-day main sleep (longest session), keyed by local WAKE-UP date.

        Keying by wake-up date (like the Mi Fitness app) keeps one entry per
        night regardless of whether bedtime was before or after midnight.
        """
        history: dict[str, dict] = {}
        for r in self._sleep_sessions.values():
            if not r.get("wakeup") or not r.get("bedtime"):
                continue
            date_str = _date_str(r["wakeup"])
            best = history.get(date_str)
            if best is None or r.get("duration", 0) > best.get("duration", 0):
                history[date_str] = {
                    "date":        date_str,
                    "bedtime":     r["bedtime"],
                    "wakeup":      r["wakeup"],
                    "duration":    r.get("duration", 0),
                    "deep":        r.get("deep", 0),
                    "light":       r.get("light", 0),
                    "rem":         r.get("rem", 0),
                    "awake_count": r.get("awake_count", 0),
                    "quality":     r.get("quality", 0),
                    "efficiency":  r.get("efficiency", 0),
                }
        cutoff_str = _days_ago_str(14)
        self._sleep_history = {k: v for k, v in history.items() if k >= cutoff_str}

    def _build_sleep_history_list(self) -> list[dict]:
        """Return sleep history sorted newest-first (max 14 entries)."""
        return sorted(
            self._sleep_history.values(),
            key=lambda x: x["date"],
            reverse=True,
        )[:14]
