#!/usr/bin/env python3
"""
CI-safe test runner for browser_scraper.py

Uses safe, public test URLs that won't trigger rate limits or legal issues.
Skips CAPTCHA tests unless CAPTCHA_API_KEY is provided.
"""
import os
import sys
import asyncio
import logging
from pathlib import Path

# Add parent dir to path to import browser_scraper
sys.path.insert(0, str(Path(__file__).parent.parent))

from browser_scraper import (
    BrowserScraper,
    ScrapeConfig,
    scrape_urls,
    save_result_to_file,
)

# Safe test URLs - public sites with minimal anti-bot (for CI testing only)
# ⚠️ NEVER use this to scrape sites you don't own without permission
CI_TEST_URLS = [
    "https://httpbin.org/html",           # Simple HTML test page
    "https://httpbin.org/headers",        # Echoes request headers
    # Uncomment below ONLY if you own the site and want to test anti-bot:
    # "https://yoursite.com/test-page",
]

async def main():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    # CI-optimized config
    config = ScrapeConfig(
        headless=True,              # Required for CI
        max_concurrent=1,           # Avoid resource contention
        delay_range=(1.0, 2.0),     # Faster for CI, but still respectful
        timeout=45_000,             # Longer timeout for CI slowness
        max_retries=2,
        
        # Anti-detection: enable but keep lightweight for CI
        rotate_user_agents=True,
        spoof_fingerprints=True,
        human_mouse=False,          # Skip for speed in CI
        simulate_scroll=False,      # Skip for speed in CI
        random_viewport_jitter=False,
        
        # CAPTCHA: only test if API key provided
        solve_captchas=bool(os.environ.get("CAPTCHA_API_KEY")),
        
        # Output
        output_format="json",
        output_file="ci_test_output",
        auto_save=True,
        save_html=False,
        extract_links=True,
        extract_images=False,
        
        # Respectful crawling
        respect_robots=True,
        max_pages_per_domain=5,
    )

    logger.info(f"Starting CI test with {len(CI_TEST_URLS)} URLs")
    logger.info(f"CAPTCHA solving: {'ENABLED' if config.solve_captchas else 'DISABLED'}")

    try:
        result = await scrape_urls(CI_TEST_URLS, config)
        
        # Save results
        save_result_to_file(result, "ci_test_output", fmt="json")
        
        # Print summary
        scraper = BrowserScraper(config)
        scraper.print_stats(result.stats)
        
        # Exit with error if all requests failed
        if result.stats["successful_requests"] == 0:
            logger.error("❌ All requests failed in CI test")
            sys.exit(1)
        
        logger.info(f"✅ CI test passed: {result.stats['successful_requests']}/{result.stats['total_requests']} successful")
        sys.exit(0)
        
    except Exception as e:
        logger.exception(f"❌ CI test failed with error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
