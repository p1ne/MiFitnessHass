# 1.3.0

## Login and setup
- After login the data API is queried and the region is detected (selected one first, default
  **auto**). A wrong region no longer causes an endless 401 → re-auth loop. Same check in
  re-auth and in manual token entry.
- **Reconfigure**: change region, forget a stored password.
- The password is stored only if "Remember password" is ticked (the long-lived passToken is used
  instead). Existing entries keep their stored password.
- Re-auth uses `async_update_reload_and_abort`; captcha/2FA steps share one implementation.
- Stable data-API client fingerprint (User-Agent) stored in the entry instead of a new random one
  on every HA restart (existing entries get one on first start).

## Polling
- Undecryptable answers / API error codes (protocol errors) are told apart from network errors:
  after 3 consecutive polls with only protocol errors the passToken refresh is tried, then
  re-auth is requested. Network errors keep backing off without re-auth.
- `has_more` from the API is honoured: when the page limit is hit the next poll comes in 30 s.
- First install: the start watermark is extrapolated from the built-in anchor (conservatively);
  if the first page shows that history would be skipped, it falls back to the anchor.
- Coordinator gets `config_entry` explicitly, so `ConfigEntryAuthFailed` reliably starts re-auth.
- Step statistics: the cumulative sum continues across gaps longer than 30 days instead of
  restarting at 0 (no negative spike).

## Sensors and UI
- "Heart Rate" shows unknown when the last reading is older than 2 h, "Stress" after 3 h
  (`last_measured` attribute still shows the old value's time).
- Sensor names via `translation_key`; Russian translation for setup and sensor names.
  entity_ids stay English (HA generates them in English for Russian), so the cards keep working.
- Diagnostics (`diagnostics.py`) with tokens, IDs and the account name redacted.
- Cards compute "today" and day keys in the HA server time zone; clock times follow the user's
  profile time-zone setting.
- `entry.runtime_data` instead of `hass.data[DOMAIN]`.
- Minimum Home Assistant version is now 2024.11 (required by the re-auth/reconfigure APIs used).

## Cleanup
- Removed unused code: `get_daily_markers`, `phone_id`, `_override_ssecurity`,
  `inject_service_token`, psecurity handling, unused imports.

## Tests
- `tests/` (pytest-homeassistant-custom-component): config flow (region detection, captcha → 2FA,
  OTP failure, duplicate account, re-auth incl. wrong account, reconfigure), coordinator
  (start-point probe, has_more, protocol vs network errors), stale sensors, diagnostics redaction,
  translations, step-statistics gap.

## Known limitation
- Hourly long-term statistics are stored by HA on UTC hour boundaries. In time zones with a
  non-whole-hour offset (e.g. India +5:30) daily step totals in statistics graphs are therefore
  shifted by 30 minutes. This is how HA statistics work and cannot be fixed in the integration;
  the "Steps Today" sensor itself is computed in local time.

# 1.2.0 — combined build (upstream batareya16 + forks Reasno + WG73)

## From WG73 (1.1.2)
- Stress sensor (0 is a valid value, measurement timestamp used).
- Long-term statistics: `mi_fitness:heart_rate_<uid>`, `mi_fitness:spo2_<uid>` (hourly mean/min/max), `mi_fitness:steps_<uid>` (hourly sum).
- Today's average/min/max heart rate is accumulated across polls instead of using only the last batch.
- State is saved after merging (it used to be saved before, so a restart lost the last poll).

## From Reasno
- Stable login `deviceId` stored in the entry + `trust=true` → 2FA is not requested on every login.
- 2FA channel chosen dynamically: SMS (option 4) or email (option 8).
- passToken rotated by Xiaomi is persisted after a silent refresh.
- All sleep sessions are kept for 30 days (`sessions_30d` attribute); incomplete records are ignored.

## New in this build
- Sleep: fragments covered by a merged/extended night are removed; the merged night replaces the
  fragment in the current sensors even if its bedtime is earlier. A nap (< 3 h) no longer
  replaces last night's sleep in the "Sleep *" sensors.
- Sleep history: one entry per day = longest sleep, keyed by **wake-up date** (like the app),
  so naps and post-midnight bedtimes no longer cause overwrites/gaps; chronotype excludes naps.
- Calories Today: records are deduped by timestamp and accumulated (previously only the last
  batch was summed, so the TOTAL_INCREASING sensor kept "resetting" and corrupted statistics).
- Day boundaries use Home Assistant's configured time zone (dt_util), not the OS/container TZ.
- Daily sensors are reset after a restart that crossed midnight; today's steps are no longer
  dropped on restart (buckets are persisted and deduped).
- After a successful silent token refresh the fetch is retried immediately; auth failures raise
  `ConfigEntryAuthFailed` (standard HA re-auth).
- passToken is also saved after a login without 2FA.
- Config flow: unique_id for password logins (same account can't be added twice); the 2FA code is
  actually requested after a captcha; captcha → 2FA works in re-auth; error shown if the code
  could not be sent; re-auth with a different account is refused.
- Logging: ssecurity/extension-pragma, STS/redirect URLs, response bodies and the signed request
  URL (contains ssecurity) are no longer written to the log; noisy WARNINGs downgraded to DEBUG.
- Large attributes (stages, history_14d, sessions_30d, hourly) excluded from the recorder DB.
- HTTP session closed on unload.

Note: `history_14d[].date` is now the wake-up date (was the bedtime date). Bundled cards work with
this and the sleep card/streak card now match "today = last night".
