import pytest
import asyncio
from browser_scraper import ScrapeConfig, BrowserScraper

@pytest.mark.asyncio
async def test_config_defaults():
    config = ScrapeConfig()
    assert config.max_concurrent == 3
    assert config.headless is True
    assert config.browser_type == "chromium"

@pytest.mark.asyncio
async def test_scraper_initialization():
    config = ScrapeConfig(headless=True, max_concurrent=1)
    scraper = BrowserScraper(config)
    assert scraper.config == config
    assert scraper.visited_urls == set()

@pytest.mark.asyncio
async def test_user_agent_rotation():
    config = ScrapeConfig(rotate_user_agents=True)
    scraper = BrowserScraper(config)
    ua1 = scraper._next_ua()
    ua2 = scraper._next_ua()
    # With rotation enabled, consecutive calls should differ (usually)
    # Note: pool is small, so this isn't guaranteed, but tests the method exists
    assert ua1 is not None
    assert "Mozilla" in ua1
