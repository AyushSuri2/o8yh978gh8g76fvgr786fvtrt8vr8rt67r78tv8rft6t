"""
browser_scraper.py
==================
Production-grade browser-based scraper for testing your own site's anti-bot
measures (Cloudflare, Datadome, hCaptcha, reCAPTCHA v2/v3, Turnstile).

Dependencies
------------
    pip install playwright playwright-stealth \
                2captcha-python \
                aiofiles beautifulsoup4 \
                Pillow requests

    playwright install chromium
    # Optional for full browser binary stealth:
    pip install undetected-playwright          # monkey-patches Playwright internals

Usage
-----
    # 1. Fill in CAPTCHA_API_KEY (2Captcha / CapSolver key) if your site uses challenges.
    # 2. Add your URLs to example_usage() or call scrape_urls() / crawl_website() directly.
    # 3. Run:  python browser_scraper.py

Architecture
------------
    BrowserScraper
    ├── StealthContext     – launches Playwright with every known stealth patch applied
    ├── FingerprintSpoofer – injects JS overrides for canvas, WebGL, fonts, AudioContext…
    ├── CaptchaSolver      – routes hCaptcha / reCAPTCHA v2/v3 / Turnstile to 2Captcha
    ├── RateLimiter        – adaptive per-domain delays with gaussian jitter
    └── DataStorage        – json / csv / sqlite output (same as before)
"""

from __future__ import annotations

import asyncio
import json
import csv
import time
import random
import logging
import hashlib
import sqlite3
import os
import base64
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Set
from collections import defaultdict, deque

import aiofiles
import requests                        # synchronous — only used by 2Captcha poller
from bs4 import BeautifulSoup, Tag
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
)
try:
    from playwright_stealth import stealth_async  # pip install playwright-stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    logging.warning(
        "playwright-stealth not installed — basic stealth only. "
        "Run: pip install playwright-stealth"
    )

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('scraper.log'), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Put your 2Captcha (or CapSolver) API key here, or set env var CAPTCHA_API_KEY.
# Leave blank to skip automatic CAPTCHA solving.
CAPTCHA_API_KEY: str = os.environ.get("CAPTCHA_API_KEY", "")


@dataclass
class ScrapeConfig:
    # Concurrency
    max_concurrent: int = 3           # browser contexts running in parallel
    delay_range: tuple = (2.0, 5.0)   # seconds between requests (per domain)

    # Browser
    headless: bool = True             # False = visible window (useful for debugging)
    browser_type: str = "chromium"    # chromium | firefox | webkit
    slow_mo: int = 0                  # ms between Playwright actions (0 = fast)
    viewport_width: int = 1920
    viewport_height: int = 1080
    locale: str = "en-US"
    timezone: str = "America/New_York"

    # Anti-detection
    rotate_user_agents: bool = True
    spoof_fingerprints: bool = True   # canvas, WebGL, AudioContext overrides
    human_mouse: bool = True          # curved mouse movements before clicks/scrolls
    simulate_scroll: bool = True      # scroll down page like a human reader
    random_viewport_jitter: bool = True  # ±10 px viewport noise

    # CAPTCHA
    solve_captchas: bool = True       # requires CAPTCHA_API_KEY

    # Crawl limits
    max_retries: int = 3
    timeout: int = 30_000             # ms (Playwright uses ms)
    respect_robots: bool = False      # set True if you want robots.txt respected
    max_pages_per_domain: int = 200
    follow_redirects: bool = True
    extract_links: bool = True
    extract_images: bool = False
    save_html: bool = False

    # Output
    output_format: str = "json"       # json | csv | sqlite
    output_file: str = "scraped_data"
    auto_save: bool = False


# ---------------------------------------------------------------------------
# Data structures (unchanged from original)
# ---------------------------------------------------------------------------

@dataclass
class ScrapedData:
    url: str
    title: str
    content: str
    metadata: Dict[str, Any]
    timestamp: datetime
    status_code: int
    response_time: float

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if isinstance(self.timestamp, datetime):
            d['timestamp'] = self.timestamp.isoformat()
        return d


@dataclass
class ScrapeResult:
    data: List[ScrapedData]
    stats: Dict[str, Any]
    failed_urls: Set[str]
    visited_urls: Set[str]

    def to_dict(self) -> Dict[str, Any]:
        s = self.stats.copy()
        for k in ('start_time', 'end_time'):
            if isinstance(s.get(k), datetime):
                s[k] = s[k].isoformat()
        return {
            'data': [i.to_dict() for i in self.data if i],
            'stats': s,
            'failed_urls': list(self.failed_urls),
            'visited_urls': list(self.visited_urls),
        }


# ---------------------------------------------------------------------------
# User-agent pool
# ---------------------------------------------------------------------------

USER_AGENTS = [
    # Chrome / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome / Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Edge / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Firefox / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    # Safari / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]


# ---------------------------------------------------------------------------
# Fingerprint spoof scripts
# These are injected as init scripts so they run before any page JS.
# ---------------------------------------------------------------------------

# Canvas fingerprint: randomise per-pixel noise so canvas.toDataURL() differs
# from a headless-default bitmap, yet still looks like a real GPU render.
CANVAS_SPOOF_JS = """
(function () {
    const orig = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (type) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const imageData = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i]     ^= Math.floor(Math.random() * 3);
                imageData.data[i + 1] ^= Math.floor(Math.random() * 3);
                imageData.data[i + 2] ^= Math.floor(Math.random() * 3);
            }
            ctx.putImageData(imageData, 0, 0);
        }
        return orig.apply(this, arguments);
    };

    const origBlob = HTMLCanvasElement.prototype.toBlob;
    HTMLCanvasElement.prototype.toBlob = function (callback, type, quality) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const imageData = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i]     ^= Math.floor(Math.random() * 3);
                imageData.data[i + 1] ^= Math.floor(Math.random() * 3);
                imageData.data[i + 2] ^= Math.floor(Math.random() * 3);
            }
            ctx.putImageData(imageData, 0, 0);
        }
        return origBlob.apply(this, arguments);
    };
})();
"""

# WebGL renderer/vendor strings — reported values that real GPUs expose
WEBGL_SPOOF_JS = """
(function () {
    const profiles = [
        { vendor: 'Google Inc. (Intel)',  renderer: 'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Google Inc. (NVIDIA)', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Google Inc. (AMD)',    renderer: 'ANGLE (AMD, AMD Radeon RX 6600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Apple',               renderer: 'Apple M2' },
    ];
    const profile = profiles[Math.floor(Math.random() * profiles.length)];

    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (param) {
        if (param === 37445) return profile.vendor;   // UNMASKED_VENDOR_WEBGL
        if (param === 37446) return profile.renderer; // UNMASKED_RENDERER_WEBGL
        return getParam.apply(this, arguments);
    };
    const getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (param) {
        if (param === 37445) return profile.vendor;
        if (param === 37446) return profile.renderer;
        return getParam2.apply(this, arguments);
    };
})();
"""

# AudioContext fingerprint noise
AUDIO_SPOOF_JS = """
(function () {
    const orig = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function () {
        const arr = orig.apply(this, arguments);
        for (let i = 0; i < arr.length; i += 100) {
            arr[i] += Math.random() * 0.0000001;
        }
        return arr;
    };
})();
"""

# Suppress webdriver / automation flags
NAVIGATOR_SPOOF_JS = """
(function () {
    // Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Spoof plugins list (headless Chrome has 0 plugins by default)
    const fakePlugins = [
        { name: 'Chrome PDF Plugin',        filename: 'internal-pdf-viewer',   description: 'Portable Document Format', length: 1 },
        { name: 'Chrome PDF Viewer',         filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '',                   length: 1 },
        { name: 'Native Client',             filename: 'internal-nacl-plugin',  description: '',                   length: 2 },
    ];
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = fakePlugins.map(p => {
                const plugin = Object.create(Plugin.prototype);
                Object.defineProperties(plugin, {
                    name:        { value: p.name },
                    filename:    { value: p.filename },
                    description: { value: p.description },
                    length:      { value: p.length },
                });
                return plugin;
            });
            arr.__proto__ = PluginArray.prototype;
            return arr;
        }
    });

    // Languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // Hardware concurrency (headless often exposes 4; real machines vary)
    const cores = [4, 8, 12, 16][Math.floor(Math.random() * 4)];
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => cores });

    // Device memory
    const mem = [4, 8, 16][Math.floor(Math.random() * 3)];
    Object.defineProperty(navigator, 'deviceMemory', { get: () => mem });

    // Permissions API — Notification permission should look undecided, not denied
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(parameters);
})();
"""

# Chrome-specific global object that headless Chrome lacks
CHROME_RUNTIME_JS = """
(function () {
    if (!window.chrome) {
        window.chrome = {
            runtime: {
                PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android',
                              CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
                PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformNaclArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
                RequestUpdateCheckStatus: { THROTTLED: 'throttled',
                                            NO_UPDATE: 'no_update',
                                            UPDATE_AVAILABLE: 'update_available' },
                OnInstalledReason: { INSTALL: 'install', UPDATE: 'update',
                                     CHROME_UPDATE: 'chrome_update',
                                     SHARED_MODULE_UPDATE: 'shared_module_update' },
                OnRestartRequiredReason: { APP_UPDATE: 'app_update',
                                           OS_UPDATE: 'os_update',
                                           PERIODIC: 'periodic' },
            },
            loadTimes: function () { return {}; },
            csi:        function () { return {}; },
        };
    }
})();
"""

ALL_SPOOF_SCRIPTS = [
    NAVIGATOR_SPOOF_JS,
    CHROME_RUNTIME_JS,
    CANVAS_SPOOF_JS,
    WEBGL_SPOOF_JS,
    AUDIO_SPOOF_JS,
]


# ---------------------------------------------------------------------------
# Human-like mouse movement helper
# ---------------------------------------------------------------------------

async def human_move_to(page: Page, x: int, y: int, steps: int = 20):
    """Move the mouse along a curved path to (x, y) to mimic human movement."""
    current = await page.evaluate("() => ({ x: window.mouseX || 0, y: window.mouseY || 0 })")
    cx, cy = current.get('x', 0), current.get('y', 0)

    # Bezier control point (adds curvature)
    cp_x = (cx + x) / 2 + random.randint(-80, 80)
    cp_y = (cy + y) / 2 + random.randint(-80, 80)

    for i in range(1, steps + 1):
        t = i / steps
        bx = int((1-t)**2 * cx + 2*(1-t)*t * cp_x + t**2 * x)
        by = int((1-t)**2 * cy + 2*(1-t)*t * cp_y + t**2 * y)
        await page.mouse.move(bx, by)
        await asyncio.sleep(random.uniform(0.005, 0.02))


async def human_scroll(page: Page):
    """Scroll down the page in a human-like fashion."""
    total_height = await page.evaluate("() => document.body.scrollHeight")
    viewport_height = await page.evaluate("() => window.innerHeight")
    current_pos = 0

    while current_pos < total_height * 0.75:
        scroll_amount = random.randint(200, 600)
        current_pos += scroll_amount
        await page.mouse.wheel(0, scroll_amount)
        await asyncio.sleep(random.uniform(0.3, 1.2))

        # Occasional short pause (reading)
        if random.random() < 0.15:
            await asyncio.sleep(random.uniform(1.5, 4.0))


# ---------------------------------------------------------------------------
# CAPTCHA solver (2Captcha API)
# ---------------------------------------------------------------------------

class CaptchaSolver:
    """
    Integrates with the 2Captcha API to solve:
      - hCaptcha
      - reCAPTCHA v2 (image checkbox)
      - reCAPTCHA v3 (score-based, invisible)
      - Cloudflare Turnstile

    Requires a 2Captcha account and API key (https://2captcha.com).
    CapSolver (https://capsolver.com) uses the same API shape — just swap the
    base_url to https://api.capsolver.com if you prefer it.
    """

    BASE_URL = "https://2captcha.com"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.poll_interval = 5       # seconds between status polls
        self.max_wait = 120          # seconds before giving up

    def _post(self, endpoint: str, data: dict) -> dict:
        data['key'] = self.api_key
        data['json'] = 1
        r = requests.post(f"{self.BASE_URL}/{endpoint}", data=data, timeout=30)
        r.raise_for_status()
        return r.json()

    def _get(self, endpoint: str, params: dict) -> dict:
        params['key'] = self.api_key
        params['json'] = 1
        r = requests.get(f"{self.BASE_URL}/{endpoint}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _poll(self, task_id: str) -> Optional[str]:
        """Poll until solved or timeout."""
        deadline = time.time() + self.max_wait
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            resp = self._get("res", {"action": "get", "id": task_id})
            if resp.get("status") == 1:
                return resp.get("request")
            if resp.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                logger.error(f"2Captcha error: {resp}")
                return None
        logger.error("CAPTCHA solve timed out")
        return None

    def solve_hcaptcha(self, site_key: str, page_url: str) -> Optional[str]:
        logger.info(f"Sending hCaptcha to 2Captcha (sitekey={site_key[:8]}…)")
        resp = self._post("in", {
            "method": "hcaptcha",
            "sitekey": site_key,
            "pageurl": page_url,
        })
        if resp.get("status") != 1:
            logger.error(f"hCaptcha submit failed: {resp}")
            return None
        return self._poll(resp["request"])

    def solve_recaptcha_v2(self, site_key: str, page_url: str) -> Optional[str]:
        logger.info(f"Sending reCAPTCHA v2 to 2Captcha (sitekey={site_key[:8]}…)")
        resp = self._post("in", {
            "method": "userrecaptcha",
            "googlekey": site_key,
            "pageurl": page_url,
        })
        if resp.get("status") != 1:
            logger.error(f"reCAPTCHA v2 submit failed: {resp}")
            return None
        return self._poll(resp["request"])

    def solve_recaptcha_v3(
        self,
        site_key: str,
        page_url: str,
        action: str = "verify",
        min_score: float = 0.7,
    ) -> Optional[str]:
        logger.info(f"Sending reCAPTCHA v3 to 2Captcha (sitekey={site_key[:8]}…)")
        resp = self._post("in", {
            "method": "userrecaptcha",
            "version": "v3",
            "googlekey": site_key,
            "pageurl": page_url,
            "action": action,
            "min_score": min_score,
        })
        if resp.get("status") != 1:
            logger.error(f"reCAPTCHA v3 submit failed: {resp}")
            return None
        return self._poll(resp["request"])

    def solve_turnstile(self, site_key: str, page_url: str) -> Optional[str]:
        """Cloudflare Turnstile challenge."""
        logger.info(f"Sending Turnstile to 2Captcha (sitekey={site_key[:8]}…)")
        resp = self._post("in", {
            "method": "turnstile",
            "sitekey": site_key,
            "pageurl": page_url,
        })
        if resp.get("status") != 1:
            logger.error(f"Turnstile submit failed: {resp}")
            return None
        return self._poll(resp["request"])


# ---------------------------------------------------------------------------
# Rate limiter (same adaptive approach as httpx version)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, default_delay: float = 2.0):
        self.domain_delays: Dict[str, float] = defaultdict(lambda: default_delay)
        self.last_request: Dict[str, float] = defaultdict(float)
        self.adaptive: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10))

    async def wait(self, domain: str, response_time: Optional[float] = None):
        now = time.time()
        elapsed = now - self.last_request[domain]
        delay = self.domain_delays[domain]

        if response_time is not None:
            self.adaptive[domain].append(response_time)
            avg = sum(self.adaptive[domain]) / len(self.adaptive[domain])
            delay = max(1.0, min(10.0, avg * 2.5))
            self.domain_delays[domain] = delay

        if elapsed < delay:
            await asyncio.sleep((delay - elapsed) + abs(random.gauss(0, 0.4)))
        self.last_request[domain] = time.time()


# ---------------------------------------------------------------------------
# Content extractor (unchanged)
# ---------------------------------------------------------------------------

class ContentExtractor:
    @staticmethod
    def extract_text(soup: BeautifulSoup) -> str:
        for el in soup(["script", "style", "nav", "footer", "aside"]):
            el.decompose()
        text = soup.get_text()
        lines = (l.strip() for l in text.splitlines())
        chunks = (p.strip() for l in lines for p in l.split("  "))
        return ' '.join(c for c in chunks if c)

    @staticmethod
    def extract_metadata(soup: BeautifulSoup) -> Dict[str, Any]:
        import re as _re
        meta: Dict[str, Any] = {}
        for tag in soup.find_all('meta'):
            if not isinstance(tag, Tag):
                continue
            name = tag.get('name') or tag.get('property') or tag.get('http-equiv')
            content = tag.get('content')
            if name and content:
                meta[str(name).lower()] = content
        og: Dict[str, Any] = {}
        for tag in soup.find_all('meta', property=_re.compile(r'^og:')):
            if isinstance(tag, Tag):
                p, c = tag.get('property'), tag.get('content')
                if p and c:
                    og[p] = c
        if og:
            meta['open_graph'] = og
        return meta

    @staticmethod
    def extract_links(soup: BeautifulSoup, base_url: str) -> List[str]:
        links = []
        for a in soup.find_all('a', href=True):
            if isinstance(a, Tag):
                href = a.get('href')
                if href:
                    links.append(urljoin(base_url, str(href).strip()))
        return list(set(links))


# ---------------------------------------------------------------------------
# Storage (unchanged)
# ---------------------------------------------------------------------------

class DataStorage:
    def __init__(self, config: ScrapeConfig):
        self.config = config
        self.data_buffer: List[ScrapedData] = []
        self.db_connection: Optional[sqlite3.Connection] = None
        if config.auto_save and config.output_format == 'sqlite':
            self._init_sqlite()

    def _init_sqlite(self):
        self.db_connection = sqlite3.connect(f"{self.config.output_file}.db")
        self.db_connection.cursor().execute('''
            CREATE TABLE IF NOT EXISTS scraped_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE, title TEXT, content TEXT,
                metadata TEXT, timestamp TEXT,
                status_code INTEGER, response_time REAL)
        ''')
        self.db_connection.commit()

    async def save_data(self, data: ScrapedData):
        if self.config.auto_save and self.config.output_format == 'sqlite':
            await self._save_sqlite(data)
        else:
            self.data_buffer.append(data)

    async def _save_sqlite(self, data: ScrapedData):
        if not self.db_connection:
            return
        try:
            self.db_connection.cursor().execute('''
                INSERT OR REPLACE INTO scraped_data
                (url, title, content, metadata, timestamp, status_code, response_time)
                VALUES (?,?,?,?,?,?,?)
            ''', (data.url, data.title, data.content,
                  json.dumps(data.metadata), data.timestamp.isoformat(),
                  data.status_code, data.response_time))
            self.db_connection.commit()
        except sqlite3.Error as e:
            logger.error(f"SQLite: {e}")

    async def flush(self):
        if not self.data_buffer or not self.config.auto_save:
            return
        if self.config.output_format == 'json':
            await self._flush_json()
        elif self.config.output_format == 'csv':
            await self._flush_csv()
        self.data_buffer.clear()

    async def _flush_json(self):
        fn = f"{self.config.output_file}.json"
        dicts = [d.to_dict() for d in self.data_buffer]
        async with aiofiles.open(fn, 'a+', encoding='utf-8') as f:
            if Path(fn).stat().st_size == 0:
                await f.write(json.dumps(dicts, indent=2, ensure_ascii=False))
            else:
                await f.seek(0, 2)
                pos = await f.tell()
                await f.seek(pos - 1)
                await f.write(',\n')
                await f.write(json.dumps(dicts, indent=2, ensure_ascii=False)[1:-1])
                await f.write('\n]')

    async def _flush_csv(self):
        fn = f"{self.config.output_file}.csv"
        exists = Path(fn).exists()
        async with aiofiles.open(fn, 'a', encoding='utf-8', newline='') as f:
            if not exists:
                await f.write('url,title,content,status_code,response_time,timestamp\n')
            for d in self.data_buffer:
                row = [f'"{d.url}"',
                       f'"{d.title.replace(chr(34), chr(34)*2)}"',
                       f'"{d.content[:500].replace(chr(34), chr(34)*2)}"',
                       str(d.status_code), str(d.response_time),
                       f'"{d.timestamp.isoformat()}"']
                await f.write(','.join(row) + '\n')


# ---------------------------------------------------------------------------
# Core browser scraper
# ---------------------------------------------------------------------------

class BrowserScraper:
    """
    Full browser-based scraper using Playwright.

    Anti-detection layers
    ---------------------
    1. playwright-stealth  – patches 25+ automation detection vectors
    2. JS init scripts     – navigator.webdriver, plugins, canvas, WebGL, Audio
    3. Realistic UA        – rotated from a pool of real browser strings
    4. Human timing        – gaussian-jitter delays, curved mouse paths, scroll
    5. CAPTCHA solving     – hCaptcha, reCAPTCHA v2/v3, Turnstile via 2Captcha
    6. Proper locale/TZ    – avoids mismatched locale fingerprint
    7. Viewport jitter     – small random variation so size isn't a constant
    """

    def __init__(self, config: Optional[ScrapeConfig] = None):
        self.config = config or ScrapeConfig()
        self.rate_limiter = RateLimiter()
        self.extractor = ContentExtractor()
        self.storage = DataStorage(self.config)
        self.captcha_solver = (
            CaptchaSolver(CAPTCHA_API_KEY)
            if CAPTCHA_API_KEY and self.config.solve_captchas
            else None
        )
        self.visited_urls: Set[str] = set()
        self.failed_urls: Set[str] = set()
        self.domain_counters: Dict[str, int] = defaultdict(int)
        self.ua_index = 0
        self.stats: Dict[str, Any] = {
            'total_requests': 0, 'successful_requests': 0,
            'failed_requests': 0, 'total_bytes': 0,
            'start_time': None, 'end_time': None,
        }

    def _next_ua(self) -> str:
        if self.config.rotate_user_agents:
            ua = USER_AGENTS[self.ua_index % len(USER_AGENTS)]
            self.ua_index += 1
            return ua
        return USER_AGENTS[0]

    def _viewport(self) -> Dict[str, int]:
        jitter = random.randint(-10, 10) if self.config.random_viewport_jitter else 0
        return {
            'width':  self.config.viewport_width  + jitter,
            'height': self.config.viewport_height + jitter,
        }

    async def _new_context(self, browser: Browser) -> BrowserContext:
        """Create a new browser context with full stealth configuration."""
        ua = self._next_ua()
        ctx = await browser.new_context(
            user_agent=ua,
            viewport=self._viewport(),
            locale=self.config.locale,
            timezone_id=self.config.timezone,
            # Accept most common content types like a real browser
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
            },
            # Randomise screen size around viewport so window.screen looks real
            screen={
                'width':  self.config.viewport_width  + random.randint(0, 200),
                'height': self.config.viewport_height + random.randint(0, 100),
            },
            java_script_enabled=True,
            ignore_https_errors=False,
            # Don't load images in headless mode to speed things up
            # (comment out if your site requires images to render properly)
            # record_video_dir=None,
        )

        # Inject all spoof scripts as init scripts (run before page JS)
        if self.config.spoof_fingerprints:
            for script in ALL_SPOOF_SCRIPTS:
                await ctx.add_init_script(script)

        return ctx

    async def _handle_captcha(self, page: Page) -> bool:
        """
        Detect and solve any CAPTCHA on the current page.
        Returns True if a CAPTCHA was found and solved (or no solver available).
        """
        if not self.captcha_solver:
            return True  # No solver configured — caller decides what to do

        url = page.url

        # --- hCaptcha ---
        hcap_el = await page.query_selector('[data-hcaptcha-sitekey], .h-captcha')
        if hcap_el:
            site_key = await hcap_el.get_attribute('data-hcaptcha-sitekey') or \
                       await hcap_el.get_attribute('data-sitekey')
            if site_key:
                token = self.captcha_solver.solve_hcaptcha(site_key, url)
                if token:
                    await page.evaluate(
                        f"document.querySelector('[name=h-captcha-response],"
                        f"[id=h-captcha-response]').value = '{token}'"
                    )
                    # Trigger hCaptcha callback if it exists
                    await page.evaluate(
                        "if(typeof hcaptchaCallback==='function') hcaptchaCallback();"
                    )
                    logger.info("hCaptcha solved and injected")
                    await page.wait_for_timeout(1500)
                    return True

        # --- reCAPTCHA v2 ---
        rcap_el = await page.query_selector('[data-sitekey]:not(.h-captcha)')
        if rcap_el:
            site_key = await rcap_el.get_attribute('data-sitekey')
            if site_key:
                token = self.captcha_solver.solve_recaptcha_v2(site_key, url)
                if token:
                    await page.evaluate(
                        f"document.getElementById('g-recaptcha-response').innerHTML='{token}';"
                    )
                    # Some sites listen for the callback; fire it
                    await page.evaluate(
                        "if(typeof ___grecaptcha_cfg!=='undefined'){"
                        "  var keys=Object.keys(___grecaptcha_cfg.clients);"
                        "  if(keys.length>0){"
                        "    var cb=___grecaptcha_cfg.clients[keys[0]].aa.aa.callback;"
                        "    if(typeof cb==='function') cb();"
                        "  }"
                        "}"
                    )
                    logger.info("reCAPTCHA v2 solved and injected")
                    await page.wait_for_timeout(1500)
                    return True

        # --- reCAPTCHA v3 (invisible — injected via execute()) ---
        has_v3 = await page.evaluate(
            "() => typeof grecaptcha !== 'undefined' && "
            "typeof grecaptcha.execute === 'function'"
        )
        if has_v3:
            # Try to find sitekey in page source
            content = await page.content()
            import re
            match = re.search(r'["\']([0-9A-Za-z_-]{40})["\']', content)
            if match:
                site_key = match.group(1)
                token = self.captcha_solver.solve_recaptcha_v3(site_key, url)
                if token:
                    # Inject token into any hidden recaptcha field
                    await page.evaluate(
                        f"const els = document.querySelectorAll('[name=\"g-recaptcha-response\"]');"
                        f"els.forEach(el => el.value = '{token}');"
                    )
                    logger.info("reCAPTCHA v3 token injected")
                    return True

        # --- Cloudflare Turnstile ---
        ts_el = await page.query_selector('[data-sitekey].cf-turnstile, .cf-turnstile')
        if ts_el:
            site_key = await ts_el.get_attribute('data-sitekey')
            if site_key:
                token = self.captcha_solver.solve_turnstile(site_key, url)
                if token:
                    await page.evaluate(
                        f"document.querySelector('[name=cf-turnstile-response]').value='{token}';"
                        f"if(typeof turnstileCallback==='function') turnstileCallback('{token}');"
                    )
                    logger.info("Turnstile solved and injected")
                    await page.wait_for_timeout(1500)
                    return True

        return True  # No captcha found — proceed normally

    async def _wait_for_cloudflare(self, page: Page, timeout: int = 30_000):
        """
        Wait for Cloudflare's JS challenge / 'Just a moment…' screen to pass.
        Cloudflare sets __cf_chl_opt in the window object once it decides to
        serve a challenge; we wait until the title changes away from the
        challenge page or the timeout expires.
        """
        try:
            # If the page title contains 'Just a moment', wait for it to change
            title = await page.title()
            if 'just a moment' in title.lower() or 'cloudflare' in title.lower():
                logger.info("Cloudflare challenge detected — waiting for resolution…")
                await page.wait_for_function(
                    "() => !document.title.toLowerCase().includes('just a moment')",
                    timeout=timeout,
                )
                logger.info("Cloudflare challenge passed")
                # Brief pause after CF clears
                await page.wait_for_timeout(random.randint(800, 2000))
        except PWTimeout:
            logger.warning("Cloudflare challenge did not resolve within timeout")

    async def _fetch_page(
        self,
        context: BrowserContext,
        url: str,
        referer: Optional[str] = None,
    ) -> Optional[ScrapedData]:
        """
        Open a URL in a new page tab with all anti-detection measures active.
        """
        domain = urlparse(url).netloc
        await self.rate_limiter.wait(domain)

        page: Optional[Page] = None
        try:
            page = await context.new_page()

            # Apply playwright-stealth if available
            if HAS_STEALTH:
                await stealth_async(page)

            if referer:
                await page.set_extra_http_headers({'Referer': referer})

            # Random mouse jiggle before navigation (looks more human)
            if self.config.human_mouse:
                await page.mouse.move(
                    random.randint(100, 800),
                    random.randint(100, 600),
                )

            start = time.time()
            response = await page.goto(
                url,
                wait_until='domcontentloaded',
                timeout=self.config.timeout,
            )
            response_time = time.time() - start

            if response is None:
                logger.warning(f"No response for {url}")
                return None

            status = response.status
            self.stats['total_requests'] += 1

            # Wait through Cloudflare JS challenge if present
            await self._wait_for_cloudflare(page)

            # Wait for network to settle (deferred JS / XHR)
            try:
                await page.wait_for_load_state('networkidle', timeout=10_000)
            except PWTimeout:
                pass  # Fine — we just want a reasonable wait

            # Detect and solve CAPTCHAs
            await self._handle_captcha(page)

            # Human-like scroll
            if self.config.simulate_scroll:
                await human_scroll(page)

            if status not in (200, 301, 302):
                logger.warning(f"HTTP {status} for {url}")
                self.stats['failed_requests'] += 1
                return None

            html = await page.content()
            self.stats['total_bytes'] += len(html.encode('utf-8'))

            if self.config.save_html:
                fn = f"html_{hashlib.md5(url.encode()).hexdigest()}.html"
                async with aiofiles.open(fn, 'w', encoding='utf-8') as f:
                    await f.write(html)

            soup = BeautifulSoup(html, 'html.parser')
            title_tag = soup.find('title')
            title = title_tag.get_text().strip() if title_tag else await page.title()

            content = self.extractor.extract_text(soup)
            metadata = self.extractor.extract_metadata(soup)
            final_url = page.url

            if self.config.extract_links:
                metadata['links'] = self.extractor.extract_links(soup, final_url)
            if self.config.extract_images:
                metadata['images'] = [
                    {'url': urljoin(final_url, img.get('src', '')),
                     'alt': img.get('alt', '')}
                    for img in soup.find_all('img', src=True)
                ]

            self.stats['successful_requests'] += 1
            await self.rate_limiter.wait(domain, response_time)
            logger.info(f"Scraped {url} ({len(content)} chars, {response_time:.2f}s)")

            return ScrapedData(
                url=final_url,
                title=title,
                content=content,
                metadata=metadata,
                timestamp=datetime.now(),
                status_code=status,
                response_time=response_time,
            )

        except PWTimeout:
            logger.error(f"Timeout fetching {url}")
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")

        self.stats['failed_requests'] += 1
        self.failed_urls.add(url)
        return None

        finally:
            if page and not page.is_closed():
                await page.close()

    async def scrape_urls(self, urls: List[str]) -> ScrapeResult:
        """Scrape a list of URLs using browser contexts."""
        self.stats['start_time'] = datetime.now()
        results: List[ScrapedData] = []

        urls_to_scrape = []
        for url in urls:
            if url in self.visited_urls:
                continue
            domain = urlparse(url).netloc
            if self.domain_counters[domain] >= self.config.max_pages_per_domain:
                continue
            urls_to_scrape.append(url)
            self.visited_urls.add(url)
            self.domain_counters[domain] += 1

        logger.info(f"Scraping {len(urls_to_scrape)} URLs with browser")
        semaphore = asyncio.Semaphore(self.config.max_concurrent)

        async with async_playwright() as pw:
            browser = await getattr(pw, self.config.browser_type).launch(
                headless=self.config.headless,
                slow_mo=self.config.slow_mo,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--disable-gpu',
                    '--window-size=1920,1080',
                    '--start-maximized',
                    # Remove 'Chrome is being controlled by automated software' bar
                    '--disable-extensions',
                    '--disable-popup-blocking',
                    '--ignore-certificate-errors',
                ],
            )

            async def _task(url: str):
                async with semaphore:
                    ctx = await self._new_context(browser)
                    try:
                        for attempt in range(1, self.config.max_retries + 1):
                            result = await self._fetch_page(ctx, url)
                            if result:
                                return result
                            if attempt < self.config.max_retries:
                                wait = (2 ** attempt) + random.uniform(1, 3)
                                logger.info(f"Retry {attempt} for {url} in {wait:.1f}s")
                                await asyncio.sleep(wait)
                        return None
                    finally:
                        await ctx.close()

            for coro in asyncio.as_completed([_task(u) for u in urls_to_scrape]):
                try:
                    result = await coro
                    if result:
                        results.append(result)
                        await self.storage.save_data(result)
                        if len(self.storage.data_buffer) >= 10:
                            await self.storage.flush()
                except Exception as e:
                    logger.error(f"Task error: {e}")

            await browser.close()

        await self.storage.flush()
        self.stats['end_time'] = datetime.now()
        self._update_stats()

        return ScrapeResult(
            data=results,
            stats=self.stats.copy(),
            failed_urls=self.failed_urls.copy(),
            visited_urls=self.visited_urls.copy(),
        )

    async def crawl_website(
        self, start_url: str, max_depth: int = 2
    ) -> ScrapeResult:
        """Crawl a website up to max_depth levels deep using browser contexts."""
        all_results: List[ScrapedData] = []
        to_visit: Set[str] = {start_url}
        start_domain = urlparse(start_url).netloc

        for depth in range(max_depth + 1):
            if not to_visit:
                break
            logger.info(f"Depth {depth}: {len(to_visit)} URLs")
            result = await self.scrape_urls(list(to_visit))
            all_results.extend([r for r in result.data if r])

            next_urls: Set[str] = set()
            for item in result.data:
                if item and 'links' in item.metadata:
                    for link in item.metadata['links']:
                        try:
                            if (urlparse(link).netloc == start_domain
                                    and link not in self.visited_urls):
                                next_urls.add(link)
                        except Exception:
                            pass
            to_visit = next_urls
            if to_visit and depth < max_depth:
                await asyncio.sleep(random.uniform(*self.config.delay_range))

        return ScrapeResult(
            data=all_results,
            stats=self.stats.copy(),
            failed_urls=self.failed_urls.copy(),
            visited_urls=self.visited_urls.copy(),
        )

    def _update_stats(self):
        s, e = self.stats.get('start_time'), self.stats.get('end_time')
        if s and e:
            dur = (e - s).total_seconds()
            self.stats['duration_seconds'] = dur
            total = self.stats['total_requests']
            if total:
                self.stats['success_rate'] = (
                    self.stats['successful_requests'] / total) * 100
            if dur:
                self.stats['requests_per_second'] = total / dur
            self.stats['total_mb'] = self.stats['total_bytes'] / 1024 / 1024
            self.stats['unique_domains'] = len(self.domain_counters)

    def print_stats(self, stats: Optional[Dict[str, Any]] = None):
        s = stats or self.stats
        if not s.get('start_time'):
            return
        logger.info("=== SCRAPING STATISTICS ===")
        logger.info(f"Duration:     {s.get('duration_seconds', 0):.2f}s")
        logger.info(f"Requests:     {s['total_requests']} / "
                    f"{s['successful_requests']} ok / {s['failed_requests']} failed")
        if s.get('success_rate') is not None:
            logger.info(f"Success rate: {s['success_rate']:.1f}%")
        logger.info(f"Data:         {s.get('total_mb', 0):.2f} MB")
        if s.get('requests_per_second') is not None:
            logger.info(f"Speed:        {s['requests_per_second']:.2f} req/s")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def scrape_urls(
    urls: List[str],
    config: Optional[ScrapeConfig] = None,
) -> ScrapeResult:
    return await BrowserScraper(config).scrape_urls(urls)


async def crawl_website(
    start_url: str,
    max_depth: int = 2,
    config: Optional[ScrapeConfig] = None,
) -> ScrapeResult:
    return await BrowserScraper(config).crawl_website(start_url, max_depth)


def save_result_to_file(result: ScrapeResult, filename: str, fmt: str = 'json'):
    if fmt == 'json':
        with open(f"{filename}.json", 'w', encoding='utf-8') as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
    elif fmt == 'csv':
        with open(f"{filename}.csv", 'w', newline='', encoding='utf-8') as f:
            if result.data:
                writer = csv.DictWriter(
                    f, fieldnames=['url', 'title', 'content',
                                   'status_code', 'response_time', 'timestamp'])
                writer.writeheader()
                for item in result.data:
                    writer.writerow({
                        'url': item.url, 'title': item.title,
                        'content': item.content[:500],
                        'status_code': item.status_code,
                        'response_time': item.response_time,
                        'timestamp': (item.timestamp.isoformat()
                                      if isinstance(item.timestamp, datetime)
                                      else item.timestamp),
                    })


# ---------------------------------------------------------------------------
# Example / smoke test
# ---------------------------------------------------------------------------

async def example_usage():
    config = ScrapeConfig(
        headless=True,              # set False to watch the browser
        max_concurrent=2,           # keep low when testing — less noise
        delay_range=(2.0, 5.0),
        solve_captchas=True,        # needs CAPTCHA_API_KEY env var
        simulate_scroll=True,
        human_mouse=True,
        rotate_user_agents=True,
        spoof_fingerprints=True,
        auto_save=False,
        extract_links=True,
    )

    urls: List[str] = [
        # Add your own site URLs here, e.g.:
        # "https://yoursite.com/",
        # "https://yoursite.com/protected-page",
    ]

    result = await scrape_urls(urls, config)
    print(f"\nScraped {len(result.data)} pages. Failed: {len(result.failed_urls)}")
    BrowserScraper(config).print_stats(result.stats)
    save_result_to_file(result, 'browser_scrape', 'json')
    return result


if __name__ == "__main__":
    # Set your 2Captcha key here OR export CAPTCHA_API_KEY=your_key_here
    # os.environ["CAPTCHA_API_KEY"] = "your_2captcha_key"

    for ext in ('.json', '.csv', '.db'):
        p = Path(f"browser_scrape{ext}")
        if p.exists():
            p.unlink()

    asyncio.run(example_usage())
