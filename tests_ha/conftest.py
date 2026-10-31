"""Fixtures for tests that run the integration inside a real Home Assistant.

These need pytest-homeassistant-custom-component, which pins its own Home
Assistant version, so they're kept apart from tests/:

    pip install pytest-homeassistant-custom-component
    pytest tests_ha
"""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    # The integration depends on the recorder, which must start before hass.
    yield
