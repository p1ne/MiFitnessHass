"""
Config flow for Mi Fitness integration.

Two setup paths:
  Path A — manual tokens: user supplies pre-obtained account credentials
  Path B — password login: the integration logs in itself
           (handles captcha and 2FA by SMS or email)

After any successful login the data API is queried to make sure the tokens
really work, and the server region is detected (the user's choice is tried
first). Without that check a wrong region produced 401s → re-auth → 401 …

Re-auth (triggered by the coordinator when tokens can't be refreshed):
  - Path B with stored password: silent re-login first
  - otherwise: interactive form
Reconfigure: change region, forget the stored password.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)

from .api import MiFitnessApiError, MiFitnessAuthError, MiFitnessClient, new_api_device_suffix
from .auth import (
    LoginResult,
    XiaomiApprovalRequired,
    XiaomiCaptchaRequired,
    XiaomiInvalidCredentials,
    XiaomiLoginError,
    XiaomiLoginSession,
)
from .const import (
    AUTH_METHOD_PASSWORD,
    AUTH_METHOD_TOKENS,
    CONF_API_DEVICE_SUFFIX,
    CONF_AUTH_METHOD,
    CONF_C_USER_ID,
    CONF_LOGIN_DEVICE_ID,
    CONF_PASS_TOKEN,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_SAVE_PASSWORD,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    CONF_USERNAME,
    DEFAULT_REGION,
    DOMAIN,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)

REGION_AUTO = "auto"
CONF_FORGET_PASSWORD = "forget_password"


class RegionDetectError(Exception):
    """No region accepted the credentials. ``reason`` is an abort/error key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _region_schema() -> vol.In:
    return vol.In([REGION_AUTO, *REGIONS])


def _probe_region(creds: dict, region: str, device_suffix: str) -> None:
    """Blocking: one tiny data-API request in ``region``. Raises on failure."""
    client = MiFitnessClient(
        user_id=creds[CONF_USER_ID],
        c_user_id=creds[CONF_C_USER_ID],
        ssecurity=creds[CONF_SSECURITY],
        service_token=creds[CONF_SERVICE_TOKEN],
        region=region,
        device_suffix=device_suffix,
    )
    try:
        client.get_health_data(0, 1)
    finally:
        client.close()


class MiFitnessConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mi Fitness."""

    VERSION = 1

    def __init__(self) -> None:
        self._login_session: XiaomiLoginSession | None = None
        self._region = REGION_AUTO
        self._username = ""
        self._password = ""
        self._save_password = False
        self._captcha_url = ""
        self._notification_url = ""
        self._device_suffix = new_api_device_suffix()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _reauth_or_none(self) -> ConfigEntry | None:
        return self._get_reauth_entry() if self.source == SOURCE_REAUTH else None

    async def _async_detect_region(self, creds: dict, preferred: str) -> str:
        """Return the first region whose data API accepts ``creds``."""
        order = [r for r in (preferred,) if r in REGIONS]
        order += [r for r in REGIONS if r not in order]
        saw_network_error = False
        for region in order:
            try:
                await self.hass.async_add_executor_job(
                    _probe_region, creds, region, self._device_suffix
                )
            except MiFitnessAuthError:
                _LOGGER.debug("Region %s: credentials rejected", region)
            except MiFitnessApiError as exc:
                _LOGGER.debug("Region %s: %s", region, exc)
                saw_network_error = True
            else:
                if region != preferred and preferred in REGIONS:
                    _LOGGER.info("Mi Fitness: data found in region %s, not %s", region, preferred)
                return region
        raise RegionDetectError("cannot_connect" if saw_network_error else "no_data_region")

    async def _async_send_otp(self) -> bool:
        """Ask Xiaomi to send the 2FA code (SMS or email). False if that failed."""
        try:
            await self.hass.async_add_executor_job(
                self._login_session.start_email_verification
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not request the verification code: %s", exc)
            return False
        return True

    async def _async_run_login(self, func, *args) -> ConfigFlowResult | LoginResult:
        """Run a login step; map 'more input needed' exceptions to the next step.

        Returns a LoginResult on success, a flow result (next form) otherwise.
        Raises XiaomiInvalidCredentials / XiaomiLoginError for the caller to show.
        """
        reauth = self._reauth_or_none() is not None
        try:
            return await self.hass.async_add_executor_job(func, *args)
        except XiaomiCaptchaRequired as exc:
            self._captcha_url = exc.captcha_url
            return await (self.async_step_reauth_captcha() if reauth else self.async_step_captcha())
        except XiaomiApprovalRequired as exc:
            self._notification_url = exc.notification_url
            if not await self._async_send_otp():
                raise XiaomiLoginError("otp_send_failed") from exc
            return await (self.async_step_reauth_approval() if reauth else self.async_step_approval())

    @staticmethod
    def _login_error_key(exc: Exception) -> str:
        if isinstance(exc, XiaomiInvalidCredentials):
            return "invalid_auth"
        if str(exc) == "otp_send_failed":
            return "otp_send_failed"
        return "login_failed"

    # ── Step 1: choose auth method ───────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show choice: manual tokens vs username/password."""
        if user_input is not None:
            self._region = user_input.get(CONF_REGION, REGION_AUTO)
            if user_input[CONF_AUTH_METHOD] == AUTH_METHOD_PASSWORD:
                return await self.async_step_password()
            return await self.async_step_tokens()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional(CONF_REGION, default=REGION_AUTO): _region_schema(),
                vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_PASSWORD): vol.In({
                    AUTH_METHOD_PASSWORD: "Password — Xiaomi account login",
                    AUTH_METHOD_TOKENS:   "Manual tokens — advanced",
                }),
            }),
        )

    # ── Path A: manual tokens ────────────────────────────────────────────────

    async def async_step_tokens(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Path A: enter pre-obtained account tokens."""
        errors: dict[str, str] = {}

        if user_input is not None:
            creds = {k: user_input[k].strip() for k in (
                CONF_USER_ID, CONF_C_USER_ID, CONF_SSECURITY, CONF_SERVICE_TOKEN
            )}
            try:
                region = await self._async_detect_region(creds, self._region)
            except RegionDetectError as exc:
                errors["base"] = "invalid_auth" if exc.reason == "no_data_region" else exc.reason
            else:
                await self.async_set_unique_id(creds[CONF_USER_ID])
                self._abort_if_unique_id_configured()
                entry_data = {
                    CONF_AUTH_METHOD:       AUTH_METHOD_TOKENS,
                    **creds,
                    CONF_REGION:            region,
                    CONF_API_DEVICE_SUFFIX: self._device_suffix,
                }
                if user_input.get(CONF_PASS_TOKEN, "").strip():
                    entry_data[CONF_PASS_TOKEN] = user_input[CONF_PASS_TOKEN].strip()
                return self.async_create_entry(
                    title=f"Mi Fitness ({creds[CONF_USER_ID]})", data=entry_data
                )

        return self.async_show_form(
            step_id="tokens",
            data_schema=vol.Schema({
                vol.Required(CONF_USER_ID): str,
                vol.Required(CONF_C_USER_ID): str,
                vol.Required(CONF_SSECURITY): str,
                vol.Required(CONF_SERVICE_TOKEN): str,
                vol.Optional(CONF_PASS_TOKEN, default=""): str,
            }),
            errors=errors,
        )

    # ── Path B: username / password ──────────────────────────────────────────

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step B1: enter Xiaomi username + password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._save_password = user_input.get(CONF_SAVE_PASSWORD, False)
            self._login_session = XiaomiLoginSession()
            try:
                result = await self._async_run_login(
                    self._login_session.start, self._username, self._password
                )
            except XiaomiLoginError as exc:
                errors["base"] = self._login_error_key(exc)
            except Exception:
                _LOGGER.exception("Unexpected login error")
                errors["base"] = "unknown"
            else:
                if not isinstance(result, LoginResult):
                    return result
                return await self._async_finish_login(result)

        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME, default=self._username): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_SAVE_PASSWORD, default=self._save_password): bool,
            }),
            errors=errors,
        )

    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step B2: user enters captcha text shown at captcha_url."""
        return await self._async_captcha_step("captcha", user_input)

    async def async_step_reauth_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_captcha_step("reauth_captcha", user_input)

    async def _async_captcha_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input.get("captcha_code", "").strip()
            try:
                result = await self._async_run_login(self._login_session.submit_captcha, code)
            except XiaomiLoginError as exc:
                errors["base"] = self._login_error_key(exc)
            else:
                if not isinstance(result, LoginResult):
                    if result.get("step_id") == step_id:   # captcha again → wrong text
                        errors["captcha_code"] = "captcha_invalid"
                    else:
                        return result
                else:
                    return await self._async_finish_login(result)

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required("captcha_code"): str}),
            errors=errors,
            description_placeholders={"captcha_url": self._captcha_url},
        )

    async def async_step_approval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step B3: 2FA code (SMS or email) — submitted via our session."""
        return await self._async_approval_step("approval", user_input)

    async def async_step_reauth_approval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_approval_step("reauth_approval", user_input)

    async def _async_approval_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            otp = (user_input.get("email_code") or "").strip()
            try:
                result = await self.hass.async_add_executor_job(
                    self._login_session.verify_with_code, otp
                )
            except XiaomiApprovalRequired:
                errors["base"] = "approval_not_detected"
            except XiaomiLoginError as exc:
                _LOGGER.warning("2FA step error: %s", exc)
                errors["base"] = "login_failed"
            else:
                return await self._async_finish_login(result)

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required("email_code"): str}),
            errors=errors,
            description_placeholders={"notification_url": self._notification_url},
        )

    # ── Finishing a login (setup or re-auth) ─────────────────────────────────

    async def _async_finish_login(self, result: LoginResult) -> ConfigFlowResult:
        reauth_entry = self._reauth_or_none()
        if reauth_entry is not None:
            return await self._async_update_entry_from_login(reauth_entry, result)

        await self.async_set_unique_id(str(result.user_id))
        self._abort_if_unique_id_configured()

        creds = {
            CONF_USER_ID:       str(result.user_id),
            CONF_C_USER_ID:     result.c_user_id,
            CONF_SSECURITY:     result.ssecurity,
            CONF_SERVICE_TOKEN: result.service_token,
        }
        try:
            region = await self._async_detect_region(creds, self._region)
        except RegionDetectError as exc:
            return self.async_abort(reason=exc.reason)

        entry_data = {
            CONF_AUTH_METHOD:       AUTH_METHOD_PASSWORD,
            **creds,
            CONF_REGION:            region,
            CONF_USERNAME:          self._username,
            CONF_SAVE_PASSWORD:     self._save_password,
            # deviceId of the trusted 2FA → reused by silent refresh / re-login
            CONF_LOGIN_DEVICE_ID:   self._login_session.device_id,
            CONF_API_DEVICE_SUFFIX: self._device_suffix,
        }
        if self._save_password:
            entry_data[CONF_PASSWORD] = self._password
        if result.pass_token:
            entry_data[CONF_PASS_TOKEN] = result.pass_token
        return self.async_create_entry(
            title=f"Mi Fitness ({result.user_id})", data=entry_data
        )

    async def _async_update_entry_from_login(
        self, entry: ConfigEntry, result: LoginResult
    ) -> ConfigFlowResult:
        """Re-auth: store fresh tokens in the existing entry and reload it."""
        old_uid = str(entry.data.get(CONF_USER_ID, ""))
        if old_uid and result.user_id and str(result.user_id) != old_uid:
            return self.async_abort(reason="wrong_account")

        # ssecurity taken from extension-pragma is session-specific: keep the
        # stored one in that case (see auth.LoginResult.ssecurity_reliable).
        ssecurity = result.ssecurity
        if not result.ssecurity_reliable and entry.data.get(CONF_SSECURITY):
            ssecurity = entry.data[CONF_SSECURITY]

        creds = {
            CONF_USER_ID:       str(result.user_id or old_uid),
            CONF_C_USER_ID:     result.c_user_id or entry.data.get(CONF_C_USER_ID, ""),
            CONF_SSECURITY:     ssecurity,
            CONF_SERVICE_TOKEN: result.service_token,
        }
        self._device_suffix = entry.data.get(CONF_API_DEVICE_SUFFIX) or self._device_suffix
        try:
            region = await self._async_detect_region(
                creds, entry.data.get(CONF_REGION, DEFAULT_REGION)
            )
        except RegionDetectError as exc:
            return self.async_abort(reason=exc.reason)

        new_data = {
            **entry.data,
            **creds,
            CONF_REGION:            region,
            CONF_USERNAME:          self._username,
            CONF_SAVE_PASSWORD:     self._save_password,
            CONF_LOGIN_DEVICE_ID:   self._login_session.device_id,
            CONF_API_DEVICE_SUFFIX: self._device_suffix,
        }
        if self._save_password:
            new_data[CONF_PASSWORD] = self._password
        else:
            new_data.pop(CONF_PASSWORD, None)
        if result.pass_token:
            new_data[CONF_PASS_TOKEN] = result.pass_token
        return self.async_update_reload_and_abort(entry, data=new_data)

    # ── Re-auth ──────────────────────────────────────────────────────────────

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Coordinator could not refresh tokens. Try a silent re-login first."""
        entry = self._get_reauth_entry()
        self._username = entry.data.get(CONF_USERNAME, "")
        self._save_password = bool(
            entry.data.get(CONF_SAVE_PASSWORD, bool(entry.data.get(CONF_PASSWORD)))
        )
        if (
            entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_PASSWORD
            and self._username
            and entry.data.get(CONF_PASSWORD)
        ):
            self._password = entry.data[CONF_PASSWORD]
            self._login_session = XiaomiLoginSession(entry.data.get(CONF_LOGIN_DEVICE_ID, ""))
            try:
                result = await self._async_run_login(
                    self._login_session.start, self._username, self._password
                )
            except XiaomiLoginError:
                pass   # stored password no longer valid → ask the user
            else:
                if not isinstance(result, LoginResult):
                    return result
                return await self._async_finish_login(result)

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Interactive re-auth form (tokens, or password)."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        method = entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_TOKENS)

        if user_input is not None:
            if method == AUTH_METHOD_TOKENS:
                creds = {
                    **{k: entry.data[k] for k in (CONF_USER_ID, CONF_C_USER_ID)},
                    CONF_SSECURITY:     user_input[CONF_SSECURITY].strip(),
                    CONF_SERVICE_TOKEN: user_input[CONF_SERVICE_TOKEN].strip(),
                }
                self._device_suffix = entry.data.get(CONF_API_DEVICE_SUFFIX) or self._device_suffix
                try:
                    region = await self._async_detect_region(
                        creds, entry.data.get(CONF_REGION, DEFAULT_REGION)
                    )
                except RegionDetectError as exc:
                    errors["base"] = "invalid_auth" if exc.reason == "no_data_region" else exc.reason
                else:
                    new_data = {**entry.data, **creds, CONF_REGION: region}
                    if user_input.get(CONF_PASS_TOKEN, "").strip():
                        new_data[CONF_PASS_TOKEN] = user_input[CONF_PASS_TOKEN].strip()
                    return self.async_update_reload_and_abort(entry, data=new_data)
            else:
                self._username = user_input[CONF_USERNAME]
                self._password = user_input[CONF_PASSWORD]
                self._save_password = user_input.get(CONF_SAVE_PASSWORD, False)
                self._login_session = XiaomiLoginSession(entry.data.get(CONF_LOGIN_DEVICE_ID, ""))
                try:
                    result = await self._async_run_login(
                        self._login_session.start, self._username, self._password
                    )
                except XiaomiLoginError as exc:
                    errors["base"] = self._login_error_key(exc)
                else:
                    if not isinstance(result, LoginResult):
                        return result
                    return await self._async_finish_login(result)

        if method == AUTH_METHOD_TOKENS:
            schema = vol.Schema({
                vol.Required(CONF_SSECURITY): str,
                vol.Required(CONF_SERVICE_TOKEN): str,
                vol.Optional(CONF_PASS_TOKEN, default=""): str,
            })
        else:
            schema = vol.Schema({
                vol.Required(CONF_USERNAME, default=self._username): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_SAVE_PASSWORD, default=self._save_password): bool,
            })
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

    # ── Reconfigure ──────────────────────────────────────────────────────────

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server region; forget a stored password."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        has_password = bool(entry.data.get(CONF_PASSWORD))

        if user_input is not None:
            creds = {k: entry.data[k] for k in (
                CONF_USER_ID, CONF_C_USER_ID, CONF_SSECURITY, CONF_SERVICE_TOKEN
            )}
            self._device_suffix = entry.data.get(CONF_API_DEVICE_SUFFIX) or self._device_suffix
            wanted = user_input[CONF_REGION]
            new_data = dict(entry.data)
            try:
                if wanted == REGION_AUTO:
                    new_data[CONF_REGION] = await self._async_detect_region(
                        creds, entry.data.get(CONF_REGION, DEFAULT_REGION)
                    )
                else:
                    await self.hass.async_add_executor_job(
                        _probe_region, creds, wanted, self._device_suffix
                    )
                    new_data[CONF_REGION] = wanted
            except RegionDetectError as exc:
                errors["base"] = exc.reason
            except MiFitnessAuthError:
                errors["base"] = "no_data_region"
            except MiFitnessApiError:
                errors["base"] = "cannot_connect"
            else:
                if user_input.get(CONF_FORGET_PASSWORD):
                    new_data.pop(CONF_PASSWORD, None)
                    new_data[CONF_SAVE_PASSWORD] = False
                return self.async_update_reload_and_abort(entry, data=new_data)

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_REGION, default=entry.data.get(CONF_REGION, DEFAULT_REGION)
            ): _region_schema(),
        }
        if has_password:
            fields[vol.Optional(CONF_FORGET_PASSWORD, default=False)] = bool
        return self.async_show_form(
            step_id="reconfigure", data_schema=vol.Schema(fields), errors=errors
        )
