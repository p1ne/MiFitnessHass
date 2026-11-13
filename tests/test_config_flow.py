"""Config flow tests with the Xiaomi login and data API mocked."""
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mi_fitness.api import MiFitnessAuthError
from custom_components.mi_fitness.auth import (
    LoginResult, XiaomiApprovalRequired, XiaomiCaptchaRequired,
)
from custom_components.mi_fitness.const import DOMAIN

RESULT = LoginResult("123", "cu", "SSEC", "STOK", pass_token="PT")


def probe_ok_in(region_ok):
    def _probe(creds, region, suffix):
        if region != region_ok:
            raise MiFitnessAuthError("401")
    return _probe


def session_mock(**kw):
    ls = MagicMock()
    ls.device_id = "an_dev"
    for k, v in kw.items():
        setattr(ls, k, v)
    return ls


async def _start_password(hass, region="auto"):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        r["flow_id"], {"region": region, "auth_method": "password"})


async def test_password_no_2fa_detects_region_and_no_password(hass):
    ls = session_mock(start=MagicMock(return_value=RESULT))
    with patch("custom_components.mi_fitness.config_flow.XiaomiLoginSession", return_value=ls), \
         patch("custom_components.mi_fitness.config_flow._probe_region", side_effect=probe_ok_in("ru")), \
         patch("custom_components.mi_fitness.async_setup_entry", return_value=True):
        r = await _start_password(hass, "de")
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    d = r["data"]
    assert d["region"] == "ru" and "password" not in d and d["save_password"] is False
    assert d["pass_token"] == "PT" and d["login_device_id"] == "an_dev" and d["api_device_suffix"]
    assert r["result"].unique_id == "123"

    # same account again → abort
    with patch("custom_components.mi_fitness.config_flow.XiaomiLoginSession", return_value=ls), \
         patch("custom_components.mi_fitness.config_flow._probe_region", side_effect=probe_ok_in("ru")):
        r = await _start_password(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"


async def test_save_password(hass):
    ls = session_mock(start=MagicMock(return_value=RESULT))
    with patch("custom_components.mi_fitness.config_flow.XiaomiLoginSession", return_value=ls), \
         patch("custom_components.mi_fitness.config_flow._probe_region", side_effect=probe_ok_in("de")), \
         patch("custom_components.mi_fitness.async_setup_entry", return_value=True):
        r = await _start_password(hass)
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"username": "u", "password": "p", "save_password": True})
    assert r["data"]["password"] == "p" and r["data"]["region"] == "de"


async def test_captcha_then_2fa_sends_code(hass):
    ls = session_mock(
        start=MagicMock(side_effect=XiaomiCaptchaRequired("https://x/captcha", "s")),
        submit_captcha=MagicMock(side_effect=[XiaomiCaptchaRequired("https://x/c2", "s"),
                                              XiaomiApprovalRequired("https://x/n", "s")]),
        start_email_verification=MagicMock(),
        verify_with_code=MagicMock(return_value=RESULT),
    )
    with patch("custom_components.mi_fitness.config_flow.XiaomiLoginSession", return_value=ls), \
         patch("custom_components.mi_fitness.config_flow._probe_region", side_effect=probe_ok_in("de")), \
         patch("custom_components.mi_fitness.async_setup_entry", return_value=True):
        r = await _start_password(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
        assert r["step_id"] == "captcha"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"captcha_code": "bad"})
        assert r["step_id"] == "captcha" and r["errors"] == {"captcha_code": "captcha_invalid"}
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"captcha_code": "good"})
        assert r["step_id"] == "approval"
        ls.start_email_verification.assert_called_once()     # the code is actually requested
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"email_code": "111111"})
    assert r["type"] is FlowResultType.CREATE_ENTRY


async def test_otp_send_failure_shows_error(hass):
    ls = session_mock(start=MagicMock(side_effect=XiaomiApprovalRequired("https://x/n", "s")),
                      start_email_verification=MagicMock(side_effect=RuntimeError("boom")))
    with patch("custom_components.mi_fitness.config_flow.XiaomiLoginSession", return_value=ls):
        r = await _start_password(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    assert r["step_id"] == "password" and r["errors"] == {"base": "otp_send_failed"}


async def test_no_region_accepts_session(hass):
    ls = session_mock(start=MagicMock(return_value=RESULT))
    with patch("custom_components.mi_fitness.config_flow.XiaomiLoginSession", return_value=ls), \
         patch("custom_components.mi_fitness.config_flow._probe_region", side_effect=MiFitnessAuthError("401")):
        r = await _start_password(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "no_data_region"


def _entry(hass, **extra):
    data = {"auth_method": "password", "user_id": "123", "c_user_id": "cu", "ssecurity": "S",
            "service_token": "T", "region": "de", "username": "u", "api_device_suffix": "a-b",
            "login_device_id": "an_old", **extra}
    e = MockConfigEntry(domain=DOMAIN, unique_id="123", data=data)
    e.add_to_hass(hass)
    return e


async def test_reauth_wrong_account(hass):
    e = _entry(hass, password="p", save_password=True)
    other = LoginResult("999", "cu", "S2", "T2")
    ls = session_mock(start=MagicMock(return_value=other))
    with patch("custom_components.mi_fitness.config_flow.XiaomiLoginSession", return_value=ls) as cls:
        r = await e.start_reauth_flow(hass)
    cls.assert_called_with("an_old")            # stored deviceId reused
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "wrong_account"


async def test_reauth_silent_success_keeps_password_choice(hass):
    e = _entry(hass, password="p", save_password=True)
    ls = session_mock(start=MagicMock(return_value=LoginResult("123", "cu", "S2", "T2", pass_token="PT2")))
    with patch("custom_components.mi_fitness.config_flow.XiaomiLoginSession", return_value=ls), \
         patch("custom_components.mi_fitness.config_flow._probe_region", side_effect=probe_ok_in("de")), \
         patch("custom_components.mi_fitness.async_setup_entry", return_value=True):
        r = await e.start_reauth_flow(hass)
    assert r["reason"] == "reauth_successful"
    assert e.data["service_token"] == "T2" and e.data["pass_token"] == "PT2" and e.data["password"] == "p"


async def test_reauth_without_stored_password_asks(hass):
    e = _entry(hass)
    r = await e.start_reauth_flow(hass)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "reauth_confirm"


async def test_reconfigure_region_and_forget_password(hass):
    e = _entry(hass, password="p", save_password=True)
    with patch("custom_components.mi_fitness.config_flow._probe_region", side_effect=probe_ok_in("sg")), \
         patch("custom_components.mi_fitness.async_setup_entry", return_value=True):
        r = await e.start_reconfigure_flow(hass)
        assert r["step_id"] == "reconfigure"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"region": "us"})
        assert r["errors"] == {"base": "no_data_region"}
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"region": "auto", "forget_password": True})
    assert r["reason"] == "reconfigure_successful"
    assert e.data["region"] == "sg" and "password" not in e.data
