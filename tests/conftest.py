import pytest
from pytest_homeassistant_custom_component.common import mock_component


@pytest.fixture(autouse=True)
def setup_env(request):
    deps = ["frontend", "http"]
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_mock")   # must come before hass
    else:
        deps.append("recorder")
    request.getfixturevalue("enable_custom_integrations")
    hass = request.getfixturevalue("hass")
    for dep in deps:
        mock_component(hass, dep)
    yield
