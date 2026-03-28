#!/usr/bin/env python3
"""
CI-safe test runner for browser_scraper.py

✅ Uses safe, public test URLs (httpbin.org, example.com)
✅ Optimized for GitHub Actions: headless, single concurrency, extended timeouts
✅ Always creates output files for artifact upload
✅ Graceful error handling with appropriate exit codes
✅ Skips CAPTCHA tests unless CAPTCHA_API_KEY is provided
✅ Detailed logging for debugging CI failures

Usage:
    python scripts/run_ci_test.py

Environment Variables:
    CAPTCHA_API_KEY: Optional 2Captcha/CapSolver key for CAPTCHA testing
    TEST_MODE: Set to "true" for CI mode (default)
    CI: Set to "true" to enable CI-specific optimizations
"""
import os
import sys
import json
import asyncio
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Add parent directory to path so we can import browser_scraper
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from browser_scraper import (
    BrowserScraper,
    ScrapeConfig,
    ScrapeResult,
    scrape_urls,
    save_result_to_file,
    logger as scraper_logger,
)

# =============================================================================
# Configuration
# =============================================================================

# Safe, public test URLs for CI testing
# ⚠️ NEVER use this script to scrape sites you don't own without permission
CI_TEST_URLS: List[str] = [
    # Simple HTML pages - fastest for CI
    "https://httpbin.org/html",
    "https://httpbin.org/headers",
    
    # Pages with JavaScript rendering (tests Playwright)
    "https://httpbin.org/delay/1",  # 1-second delay to test timeout handling
    
    # Example domains (IANA reserved for documentation)
    "https://example.com",
    "https://example.org",
    
    # Uncomment below ONLY for testing your own anti-bot measures:
    # "https://yoursite.com/test-page",
    # "https://yoursite.com/protected-endpoint",
]

# Output configuration
OUTPUT_BASE: str = "ci_test_output"
LOG_FILE: str = "scraper.log"
TIMEOUT_SECONDS: int = 300  # Max total runtime for CI test


# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging() -> logging.Logger:
    """Configure logging for CI environment."""
    # Clear any existing handlers to avoid duplicates
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    
    # Create logger
    logger = logging.getLogger("ci_test")
    logger.setLevel(logging.INFO)
    
    # Console handler (for GitHub Actions log output)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler (for artifact upload)
    try:
        file_handler = logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)
    except Exception as e:
        logger.warning(f"Could not create log file handler: {e}")
    
    return logger


# =============================================================================
# CI-Optimized Configuration
# =============================================================================

def get_ci_config() -> ScrapeConfig:
    """
    Return a ScrapeConfig optimized for CI/CD environments.
    
    Key optimizations:
    - headless=True: Required for non-GUI runners
    - max_concurrent=1: Avoid resource contention on shared runners
    - Extended timeouts: Account for CI slowness
    - Disabled human simulation: Faster execution, less flakiness
    - CAPTCHA solving: Only enabled if API key is provided
    """
    # Check if CAPTCHA solving should be enabled
    captcha_key = os.environ.get("CAPTCHA_API_KEY", "").strip()
    solve_captchas = bool(captcha_key and captcha_key != "your_2captcha_key_here")
    
    # Check if we're in CI mode (for additional optimizations)
    is_ci = os.environ.get("CI", "").lower() == "true" or \
            os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    
    config = ScrapeConfig(
        # === Browser Settings ===
        headless=True,                          # Required for CI
        browser_type="chromium",                # Fastest, most compatible
        slow_mo=0,                              # No artificial delays
        viewport_width=1280,                    # Standard CI viewport
        viewport_height=720,
        locale="en-US",
        timezone="UTC",                         # Consistent timezone
        
        # === Concurrency & Rate Limiting ===
        max_concurrent=1,                       # Avoid runner resource exhaustion
        delay_range=(0.5, 1.5) if is_ci else (2.0, 5.0),  # Faster in CI
        
        # === Anti-Detection (Lightweight for CI) ===
        rotate_user_agents=True,                # Basic UA rotation
        spoof_fingerprints=True,                # Enable JS spoofing
        human_mouse=False,                      # Skip for speed in CI
        simulate_scroll=False,                  # Skip for speed in CI
        random_viewport_jitter=False,           # Deterministic for CI
        
        # === CAPTCHA Handling ===
        solve_captchas=solve_captchas,          # Only if key provided
        
        # === Reliability ===
        max_retries=2,                          # Fewer retries = faster CI
        timeout=45_000,                         # 45s per page (CI can be slow)
        respect_robots=True,                    # Be respectful even in tests
        max_pages_per_domain=3,                 # Limit per-domain requests
        follow_redirects=True,
        
        # === Output ===
        output_format="json",
        output_file=OUTPUT_BASE,
        auto_save=True,                         # Save incrementally
        save_html=False,                        # Skip to save space/time
        extract_links=True,
        extract_images=False,                   # Skip to save bandwidth
    )
    
    return config


# =============================================================================
# Output File Management
# =============================================================================

def ensure_output_files(
    result: Optional[ScrapeResult],
    config: ScrapeConfig,
    error: Optional[Exception] = None
) -> None:
    """
    Guarantee that output files exist for artifact upload.
    
    This is critical for CI: even if the scraper fails completely,
    we need at least one file to upload so we can debug what happened.
    """
    output_base = Path(config.output_file)
    timestamp = datetime.now().isoformat()
    
    # === JSON Output ===
    json_path = output_base.with_suffix('.json')
    if not json_path.exists() or json_path.stat().st_size == 0:
        output_data = {
            "ci_test_metadata": {
                "status": "error" if error else "completed",
                "timestamp": timestamp,
                "test_urls": CI_TEST_URLS,
                "error": str(error) if error else None,
                "traceback": traceback.format_exc() if error else None,
            },
            "scraper_stats": result.stats if result and hasattr(result, 'stats') else {},
            "data": [],
            "failed_urls": list(result.failed_urls) if result and hasattr(result, 'failed_urls') else [],
            "visited_urls": list(result.visited_urls) if result and hasattr(result, 'visited_urls') else [],
        }
        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False, default=str)
            print(f"✓ Created fallback output: {json_path}")
        except Exception as e:
            print(f"✗ Failed to write JSON output: {e}")
    
    # === Log File (if not already created) ===
    log_path = Path(LOG_FILE)
    if not log_path.exists() or log_path.stat().st_size == 0:
        try:
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(f"CI Test Log - {timestamp}\n")
                f.write(f"Status: {'ERROR' if error else 'COMPLETED'}\n")
                f.write(f"Test URLs: {CI_TEST_URLS}\n")
                if error:
                    f.write(f"\nError: {error}\n")
                    f.write(f"\nTraceback:\n{traceback.format_exc()}\n")
            print(f"✓ Created fallback log: {log_path}")
        except Exception as e:
            print(f"✗ Failed to write log file: {e}")
    
    # === Summary File (easy to read in GitHub UI) ===
    summary_path = output_base.with_suffix('.summary.txt')
    try:
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(f"CI Scraper Test Summary\n")
            f.write(f"{'='*50}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Status: {'✅ PASSED' if not error else '❌ FAILED'}\n")
            f.write(f"URLs Tested: {len(CI_TEST_URLS)}\n")
            
            if result:
                stats = result.stats
                f.write(f"Successful Requests: {stats.get('successful_requests', 0)}\n")
                f.write(f"Failed Requests: {stats.get('failed_requests', 0)}\n")
                f.write(f"Total Requests: {stats.get('total_requests', 0)}\n")
                if stats.get('duration_seconds'):
                    f.write(f"Duration: {stats['duration_seconds']:.1f}s\n")
                if stats.get('success_rate') is not None:
                    f.write(f"Success Rate: {stats['success_rate']:.1f}%\n")
            
            if error:
                f.write(f"\nError Details:\n{error}\n")
            
            f.write(f"\nTest URLs:\n")
            for url in CI_TEST_URLS:
                f.write(f"  - {url}\n")
        
        print(f"✓ Created summary: {summary_path}")
    except Exception as e:
        print(f"✗ Failed to write summary: {e}")


# =============================================================================
# Main Test Function
# =============================================================================

async def run_ci_test() -> int:
    """
    Run the CI test and return exit code.
    
    Returns:
        0: Success (at least one URL scraped successfully)
        1: Failure (all URLs failed or critical error)
        2: Configuration error (missing dependencies, etc.)
    """
    logger = setup_logging()
    logger.info("🚀 Starting CI scraper test")
    logger.info(f"📋 Testing {len(CI_TEST_URLS)} URLs")
    logger.info(f"🔐 CAPTCHA solving: {'ENABLED' if os.environ.get('CAPTCHA_API_KEY') else 'DISABLED'}")
    logger.info(f"🖥️  CI mode: {os.environ.get('CI', 'false').upper()}")
    
    config = get_ci_config()
    result: Optional[ScrapeResult] = None
    error: Optional[Exception] = None
    
    try:
        # Validate configuration
        if not CI_TEST_URLS:
            raise ValueError("No test URLs configured")
        
        # Initialize scraper
        logger.info("🔧 Initializing BrowserScraper...")
        scraper = BrowserScraper(config)
        
        # Run the scrape
        logger.info("🌐 Starting URL scraping...")
        start_time = datetime.now()
        
        result = await scraper.scrape_urls(CI_TEST_URLS)
        
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"⏱️  Scraping completed in {elapsed:.1f}s")
        
        # Print stats
        if result:
            scraper.print_stats(result.stats)
            
            # Save results using the scraper's built-in function
            try:
                save_result_to_file(result, config.output_file, fmt=config.output_format)
                logger.info(f"💾 Results saved to {config.output_file}.{config.output_format}")
            except Exception as e:
                logger.warning(f"⚠️  Could not save results via save_result_to_file: {e}")
        
        # Determine success
        if result and result.stats.get('successful_requests', 0) > 0:
            logger.info("✅ CI test PASSED: At least one URL scraped successfully")
            return 0
        else:
            logger.warning("⚠️  CI test WARNING: No successful requests")
            return 1
            
    except ImportError as e:
        logger.error(f"❌ Configuration error: Missing dependency - {e}")
        error = e
        return 2
        
    except Exception as e:
        logger.error(f"❌ CI test FAILED: {type(e).__name__}: {e}")
        logger.debug("Full traceback:", exc_info=True)
        error = e
        return 1
        
    finally:
        # ALWAYS ensure output files exist for artifact upload
        logger.info("📦 Ensuring output files for artifact upload...")
        ensure_output_files(result, config, error)
        
        # Print final status to stdout (visible in GitHub Actions log)
        status = "✅ PASSED" if (error is None and result and result.stats.get('successful_requests', 0) > 0) else "❌ FAILED"
        print(f"\n{'='*60}")
        print(f"CI TEST RESULT: {status}")
        print(f"{'='*60}")
        if result and hasattr(result, 'stats'):
            print(f"Successful: {result.stats.get('successful_requests', 0)}")
            print(f"Failed: {result.stats.get('failed_requests', 0)}")
            print(f"Total: {result.stats.get('total_requests', 0)}")
        print(f"{'='*60}\n")


# =============================================================================
# Entry Point
# =============================================================================

def main() -> None:
    """Synchronous entry point for the CI test script."""
    # Set environment defaults for CI
    if os.environ.get("TEST_MODE", "").lower() == "true":
        os.environ.setdefault("CI", "true")
    
    # Run async main
    exit_code = asyncio.run(run_ci_test())
    
    # Exit with appropriate code for GitHub Actions
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
