import os
from pathlib import Path

import pytest


def _browser_available():
    """Bundled Playwright Chromium, else (dev verification only) the installed Chrome channel."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    if os.environ.get('BTC_PLAYWRIGHT_CHANNEL'):
        return os.environ['BTC_PLAYWRIGHT_CHANNEL']
    with sync_playwright() as p:
        if Path(p.chromium.executable_path).exists():
            return ''
    if Path('/Applications/Google Chrome.app').exists():
        return 'chrome'
    return None


@pytest.fixture(scope='session')
def browser_channel():
    channel = _browser_available()
    if channel is None:
        pytest.skip('No Chromium for Playwright on this machine')
    return channel


@pytest.fixture
def real_browser(browser_channel, monkeypatch):
    if browser_channel:
        monkeypatch.setenv('BTC_PLAYWRIGHT_CHANNEL', browser_channel)
    return browser_channel
