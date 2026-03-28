# advanced_scraper.py

import asyncio
import aiohttp
import aiofiles
import json
import csv
import time
import random
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Set, Any, cast, Tuple
from contextlib import asynccontextmanager
import logging
import hashlib
from bs4 import BeautifulSoup, Tag
import re
from fake_useragent import UserAgent
from urllib.robotparser import RobotFileParser
import sqlite3
from collections import defaultdict, deque
import backoff
import ssl
import certifi

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scraper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class ScrapedData:
    """Data structure for scraped content"""
    url: str
    title: str
    content: str
    metadata: Dict[str, Any]
    timestamp: datetime
    status_code: int
    response_time: float
    
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if isinstance(self.timestamp, datetime):
            data['timestamp'] = self.timestamp.isoformat()
        return data

@dataclass
class ScrapeConfig:
    """Configuration for scraping operations"""
    max_concurrent: int = 100
    delay_range: tuple = (1, 3)
    max_retries: int = 3
    timeout: int = 30
    respect_robots: bool = True
    max_pages_per_domain: int = 100
    follow_redirects: bool = True
    extract_images: bool = False
    extract_links: bool = True
    save_html: bool = False
    output_format: str = 'json'  # json, csv, sqlite
    output_file: str = 'scraped_data'
    auto_save: bool = False  # Whether to automatically save to files

@dataclass
class ScrapeResult:
    """Result container for scraping operations"""
    data: List[ScrapedData]
    stats: Dict[str, Any]
    failed_urls: Set[str]
    visited_urls: Set[str]
    
    def to_dict(self) -> Dict[str, Any]:
        # Serialize stats datetimes
        serializable_stats = self.stats.copy()
        if 'start_time' in serializable_stats and isinstance(serializable_stats['start_time'], datetime):
            serializable_stats['start_time'] = serializable_stats['start_time'].isoformat()
        if 'end_time' in serializable_stats and isinstance(serializable_stats['end_time'], datetime):
            serializable_stats['end_time'] = serializable_stats['end_time'].isoformat()

        return {
            'data': [item.to_dict() for item in self.data if item],
            'stats': serializable_stats,
            'failed_urls': list(self.failed_urls),
            'visited_urls': list(self.visited_urls)
        }

class RateLimiter:
    """Advanced rate limiter with per-domain tracking"""
    
    def __init__(self, default_delay: float = 1.0):
        self.domain_delays = defaultdict(lambda: default_delay)
        self.last_request = defaultdict(float)
        self.request_counts = defaultdict(int)
        self.adaptive_delays = defaultdict(lambda: deque(maxlen=10))
    
    async def wait(self, domain: str, response_time: Optional[float] = None):
        """Wait appropriate time before next request to domain"""
        now = time.time()
        time_since_last = now - self.last_request[domain]
        
        # Use a non-adaptive delay if no response time is available yet
        delay = self.domain_delays.get(domain, 1.0)
        
        if response_time is not None:
            self.adaptive_delays[domain].append(response_time)
            avg_response = sum(self.adaptive_delays[domain]) / len(self.adaptive_delays[domain])
            # Adaptive delay based on server response time
            delay = max(0.5, min(5.0, avg_response * 2))
            self.domain_delays[domain] = delay
        
        if time_since_last < delay:
            sleep_time = delay - time_since_last + random.uniform(0.1, 0.5)
            await asyncio.sleep(sleep_time)
        
        self.last_request[domain] = time.time()
        self.request_counts[domain] += 1

class SessionManager:
    """Manages HTTP sessions with connection pooling and cookie persistence"""
    
    def __init__(self, max_connections: int = 100):
        self.max_connections = max_connections
        self.user_agent = UserAgent()
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        
    @asynccontextmanager
    async def get_session(self):
        """Get configured aiohttp session"""
        connector = aiohttp.TCPConnector(
            limit=self.max_connections,
            limit_per_host=20,
            ssl=self.ssl_context,
            use_dns_cache=True,
            ttl_dns_cache=300
        )
        
        headers = {
            'User-Agent': self.user_agent.random,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        
        async with aiohttp.ClientSession(
            connector=connector,
            headers=headers,
            timeout=timeout,
            cookie_jar=aiohttp.CookieJar()
        ) as session:
            yield session

class RobotsChecker:
    """Check robots.txt compliance"""
    
    def __init__(self):
        self.robots_cache: Dict[str, RobotFileParser] = {}
        self.cache_expiry: Dict[str, datetime] = {}
    
    def can_fetch(self, url: str, user_agent: str = '*') -> bool:
        """Check if URL can be fetched according to robots.txt"""
        try:
            parsed = urlparse(url)
            # Ensure we have a scheme for robots.txt
            scheme = parsed.scheme if parsed.scheme else "https"
            robots_url = f"{scheme}://{parsed.netloc}/robots.txt"
            
            if robots_url in self.robots_cache and datetime.now() < self.cache_expiry[robots_url]:
                rp = self.robots_cache[robots_url]
                return rp.can_fetch(user_agent, url)
            
            rp = RobotFileParser()
            rp.set_url(robots_url)
            # Note: rp.read() is synchronous. For a fully async app, this could be improved.
            rp.read()
            
            self.robots_cache[robots_url] = rp
            self.cache_expiry[robots_url] = datetime.now() + timedelta(hours=1)
            
            return rp.can_fetch(user_agent, url)
        except Exception as e:
            logger.warning(f"Error checking robots.txt for {url}: {e}")
            return True

class ContentExtractor:
    """Extract and process content from HTML"""
    
    @staticmethod
    def extract_text(soup: BeautifulSoup) -> str:
        """Extract clean text content"""
        for element in soup(["script", "style", "nav", "footer", "aside"]):
            element.decompose()
        
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        return ' '.join(chunk for chunk in chunks if chunk)
    
    @staticmethod
    def extract_metadata(soup: BeautifulSoup) -> Dict[str, Any]:
        """Extract metadata from HTML"""
        metadata: Dict[str, Any] = {}
        
        for meta in soup.find_all('meta'):
            # Ensure we're dealing with a Tag object, not a NavigableString
            if isinstance(meta, Tag):
                name = meta.get('name') or meta.get('property') or meta.get('http-equiv')
                content = meta.get('content')
                if name and content:
                    metadata[str(name).lower()] = content
        
        og_tags = {}
        for meta in soup.find_all('meta', property=re.compile(r'^og:')):
            if isinstance(meta, Tag):
                property_name = meta.get('property')
                content = meta.get('content')
                if property_name and content:
                    og_tags[property_name] = content
        if og_tags:
            metadata['open_graph'] = og_tags
            
        structured_data = []
        for script in soup.find_all('script', type='application/ld+json'):
            if isinstance(script, Tag) and script.string:
                try:
                    data = json.loads(script.string)
                    structured_data.append(data)
                except (json.JSONDecodeError, AttributeError):
                    continue
                
        if structured_data:
            metadata['structured_data'] = structured_data
            
        return metadata
    
    @staticmethod
    def extract_links(soup: BeautifulSoup, base_url: str) -> List[str]:
        """Extract all links from page"""
        links = []
        for link in soup.find_all('a', href=True):
            if isinstance(link, Tag):
                href = cast(Optional[str], link.get('href'))
                if href:
                    absolute_url = urljoin(base_url, href.strip())
                    links.append(absolute_url)
        return list(set(links))
    
    @staticmethod
    def extract_images(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
        """Extract image information"""
        images = []
        for img in soup.find_all('img'):
            if isinstance(img, Tag):
                src = cast(Optional[str], img.get('src'))
                if src:
                    absolute_url = urljoin(base_url, src.strip())
                    images.append({
                        'url': absolute_url,
                        'alt': cast(str, img.get('alt', '')),
                        'title': cast(str, img.get('title', ''))
                    })
        return images

class DataStorage:
    """Handle data storage in various formats"""
    
    def __init__(self, config: ScrapeConfig):
        self.config = config
        self.data_buffer: List[ScrapedData] = []
        self.db_connection: Optional[sqlite3.Connection] = None
        
        if config.auto_save and config.output_format == 'sqlite':
            self._init_sqlite()
    
    def _init_sqlite(self):
        db_path = f"{self.config.output_file}.db"
        self.db_connection = sqlite3.connect(db_path)
        cursor = self.db_connection.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scraped_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                title TEXT,
                content TEXT,
                metadata TEXT,
                timestamp TEXT,
                status_code INTEGER,
                response_time REAL
            )
        ''')
        self.db_connection.commit()
    
    async def save_data(self, data: ScrapedData):
        if self.config.auto_save and self.config.output_format == 'sqlite':
            await self._save_to_sqlite(data)
        else:
            self.data_buffer.append(data)
    
    async def _save_to_sqlite(self, data: ScrapedData):
        if not self.db_connection:
            logger.error("SQLite database not connected. Cannot save data.")
            return
        
        try:
            cursor = self.db_connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO scraped_data 
                (url, title, content, metadata, timestamp, status_code, response_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                data.url,
                data.title,
                data.content,
                json.dumps(data.metadata),
                data.timestamp.isoformat(),
                data.status_code,
                data.response_time
            ))
            self.db_connection.commit()
        except sqlite3.Error as e:
            logger.error(f"Error saving to SQLite: {e}")
    
    async def flush_buffer(self):
        if not self.data_buffer or not self.config.auto_save:
            return
        
        if self.config.output_format == 'json':
            await self._save_json()
        elif self.config.output_format == 'csv':
            await self._save_csv()
        
        self.data_buffer.clear()
    
    async def _save_json(self):
        filename = f"{self.config.output_file}.json"
        data_dicts = [data.to_dict() for data in self.data_buffer]
        
        # Ensure timestamp is string in saved json
        for item in data_dicts:
            if isinstance(item.get('timestamp'), datetime):
                item['timestamp'] = item['timestamp'].isoformat()

        async with aiofiles.open(filename, 'a+', encoding='utf-8') as f:
            if Path(filename).stat().st_size == 0:
                await f.write(json.dumps(data_dicts, indent=2, ensure_ascii=False))
            else:
                await f.seek(0, 2)  # Move to the end
                pos = await f.tell()
                await f.seek(pos - 1)  # Move back one character from end (over ']')
                await f.write(',\n')
                await f.write(json.dumps(data_dicts, indent=2, ensure_ascii=False)[1:-1])
                await f.write('\n]')

    
    async def _save_csv(self):
        filename = f"{self.config.output_file}.csv"
        
        if self.data_buffer:
            fieldnames = ['url', 'title', 'content', 'status_code', 'response_time', 'timestamp']
            
            file_exists = Path(filename).exists()
            
            async with aiofiles.open(filename, 'a', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
                
                # Write header only if file is new
                # To do this async, we have to write it manually
                if not file_exists:
                    await f.write(','.join(fieldnames) + '\n')
                
                for data in self.data_buffer:
                    title_escaped = data.title.replace('"', '""')
                    content_escaped = data.content[:500].replace('"', '""')
                    row_items = [
                        f'"{data.url}"',
                        f'"{title_escaped}"',
                        f'"{content_escaped}"',
                        str(data.status_code),
                        str(data.response_time),
                        f'"{data.timestamp.isoformat()}"'
                    ]
                    await f.write(','.join(row_items) + '\n')

class AdvancedWebScraper:
    
    def __init__(self, config: Optional[ScrapeConfig] = None):
        self.config = config or ScrapeConfig()
        self.rate_limiter = RateLimiter()
        self.session_manager = SessionManager(self.config.max_concurrent)
        self.robots_checker = RobotsChecker()
        self.content_extractor = ContentExtractor()
        self.storage = DataStorage(self.config)
        
        self.visited_urls: Set[str] = set()
        self.failed_urls: Set[str] = set()
        self.domain_counters = defaultdict(int)
        
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'total_bytes': 0,
            'start_time': None,
            'end_time': None,
        }
    
    @backoff.on_exception(
        backoff.expo,
        (aiohttp.ClientError, asyncio.TimeoutError),
        max_tries=3,
        max_time=60
    )
    async def _fetch_url(self, session: aiohttp.ClientSession, url: str) -> Optional[ScrapedData]:
        # Normalize URL scheme if missing
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
            
        domain = urlparse(url).netloc
        
        if self.config.respect_robots and not self.robots_checker.can_fetch(url):
            logger.info(f"Robots.txt disallows fetching {url}")
            return None
        
        await self.rate_limiter.wait(domain)
        start_time = time.time()
        
        try:
            async with session.get(url, allow_redirects=self.config.follow_redirects) as response:
                response_time = time.time() - start_time
                await self.rate_limiter.wait(domain, response_time) # Update adaptive rate limiter

                self.stats['total_requests'] += 1
                
                if response.status != 200:
                    logger.warning(f"HTTP {response.status} for {url}")
                    self.stats['failed_requests'] += 1
                    return None
                
                content = await response.read()
                self.stats['total_bytes'] += len(content)
                text_content = content.decode('utf-8', errors='ignore')
                
                soup = BeautifulSoup(text_content, 'html.parser')
                
                title_tag = soup.find('title')
                title = title_tag.get_text().strip() if title_tag else ''
                
                clean_content = self.content_extractor.extract_text(soup)
                metadata = self.content_extractor.extract_metadata(soup)
                
                if self.config.extract_links:
                    metadata['links'] = self.content_extractor.extract_links(soup, str(response.url))
                
                if self.config.extract_images:
                    metadata['images'] = self.content_extractor.extract_images(soup, str(response.url))
                
                if self.config.save_html:
                    html_filename = f"html_{hashlib.md5(url.encode()).hexdigest()}.html"
                    async with aiofiles.open(html_filename, 'w', encoding='utf-8') as f:
                        await f.write(text_content)
                    metadata['html_file'] = html_filename
                
                scraped_data = ScrapedData(
                    url=str(response.url), # Use final URL after redirects
                    title=title,
                    content=clean_content,
                    metadata=metadata,
                    timestamp=datetime.now(),
                    status_code=response.status,
                    response_time=response_time
                )
                
                self.stats['successful_requests'] += 1
                logger.info(f"Successfully scraped {url} ({len(clean_content)} chars)")
                
                return scraped_data
                
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            self.stats['failed_requests'] += 1
            self.failed_urls.add(url)
            return None
    
    async def scrape_urls(self, urls: List[str]) -> ScrapeResult:
        """Scrape multiple URLs and return results with stats"""
        self.stats['start_time'] = datetime.now()
        results = []
        
        urls_to_scrape = []
        for url in urls:
            if url in self.visited_urls:
                continue
            
            domain = urlparse(url).netloc
            if self.domain_counters[domain] >= self.config.max_pages_per_domain:
                logger.info(f"Max pages reached for domain {domain}")
                continue
            
            urls_to_scrape.append(url)
            self.visited_urls.add(url)
            self.domain_counters[domain] += 1
        
        logger.info(f"Starting to scrape {len(urls_to_scrape)} URLs")
        
        async with self.session_manager.get_session() as session:
            semaphore = asyncio.Semaphore(self.config.max_concurrent)
            
            async def scrape_with_semaphore(url):
                async with semaphore:
                    return await self._fetch_url(session, url)
            
            tasks = [scrape_with_semaphore(url) for url in urls_to_scrape]
            
            for coro in asyncio.as_completed(tasks):
                try:
                    result = await coro
                    if result:
                        results.append(result)
                        await self.storage.save_data(result)
                        
                        if len(self.storage.data_buffer) >= 10:
                            await self.storage.flush_buffer()
                            
                except Exception as e:
                    logger.error(f"Task failed: {e}")
        
        await self.storage.flush_buffer()
        
        self.stats['end_time'] = datetime.now()
        self._update_stats_with_duration()
        
        return ScrapeResult(
            data=results,
            stats=self.stats.copy(),
            failed_urls=self.failed_urls.copy(),
            visited_urls=self.visited_urls.copy()
        )
    
    async def crawl_website(self, start_url: str, max_depth: int = 2) -> ScrapeResult:
        """Crawl a website starting from a URL up to max_depth"""
        all_results: List[ScrapedData] = []
        to_visit = {start_url}
        
        for depth in range(max_depth + 1):
            if not to_visit:
                break
            
            logger.info(f"Crawling depth {depth} with {len(to_visit)} URLs")
            
            result = await self.scrape_urls(list(to_visit))
            all_results.extend([res for res in result.data if res]) # Filter out None
            
            next_urls = set()
            for item in result.data:
                if item and 'links' in item.metadata:
                    for link in item.metadata['links']:
                        try:
                            if urlparse(link).netloc == urlparse(start_url).netloc:
                                if link not in self.visited_urls:
                                    next_urls.add(link)
                        except Exception:
                            continue # Ignore malformed links
            
            to_visit = next_urls
            
            if to_visit and depth < max_depth:
                await asyncio.sleep(random.uniform(*self.config.delay_range))
        
        # Return final result with all crawled data
        return ScrapeResult(
            data=all_results,
            stats=self.stats.copy(),
            failed_urls=self.failed_urls.copy(),
            visited_urls=self.visited_urls.copy()
        )
    
    def _update_stats_with_duration(self):
        """Update stats with computed values"""
        if self.stats['start_time'] and self.stats['end_time']:
            duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()
            self.stats['duration_seconds'] = duration
            
            if self.stats['total_requests'] > 0:
                self.stats['success_rate'] = (self.stats['successful_requests'] / self.stats['total_requests']) * 100
                
            if duration > 0:
                self.stats['requests_per_second'] = self.stats['total_requests'] / duration
                
            self.stats['total_mb'] = self.stats['total_bytes'] / 1024 / 1024
            self.stats['unique_domains'] = len(self.domain_counters)
    
    def print_stats(self, stats: Optional[Dict[str, Any]] = None):
        """Print scraping statistics"""
        stats_to_print = stats or self.stats
        
        if not stats_to_print.get('start_time') or not stats_to_print.get('end_time'):
            logger.warning("Scraping did not run, no statistics to show.")
            return

        logger.info("=== SCRAPING STATISTICS ===")
        logger.info(f"Duration: {stats_to_print.get('duration_seconds', 0):.2f} seconds")
        logger.info(f"Total requests: {stats_to_print['total_requests']}")
        logger.info(f"Successful: {stats_to_print['successful_requests']}")
        logger.info(f"Failed: {stats_to_print['failed_requests']}")
        
        if stats_to_print.get('success_rate') is not None:
            logger.info(f"Success rate: {stats_to_print['success_rate']:.1f}%")
            
        logger.info(f"Total data: {stats_to_print.get('total_mb', 0):.2f} MB")
        
        if stats_to_print.get('requests_per_second') is not None:
            logger.info(f"Average speed: {stats_to_print['requests_per_second']:.2f} requests/sec")
            
        logger.info(f"Unique domains: {stats_to_print.get('unique_domains', 0)}")


# Main functions for easy import and use
async def scrape_urls(urls: List[str], config: Optional[ScrapeConfig] = None) -> ScrapeResult:
    """
    Simple function to scrape multiple URLs
    
    Args:
        urls: List of URLs to scrape
        config: Optional configuration object
        
    Returns:
        ScrapeResult containing data, stats, and metadata
    """
    scraper = AdvancedWebScraper(config)
    return await scraper.scrape_urls(urls)

async def crawl_website(start_url: str, max_depth: int = 2, config: Optional[ScrapeConfig] = None) -> ScrapeResult:
    """
    Simple function to crawl a website
    
    Args:
        start_url: Starting URL for crawling
        max_depth: Maximum crawl depth
        config: Optional configuration object
        
    Returns:
        ScrapeResult containing data, stats, and metadata
    """
    scraper = AdvancedWebScraper(config)
    return await scraper.crawl_website(start_url, max_depth)

def save_result_to_file(result: ScrapeResult, filename: str, format: str = 'json'):
    """
    Save scrape result to file
    
    Args:
        result: ScrapeResult object to save
        filename: Output filename (without extension)
        format: Output format ('json', 'csv')
    """
    if format == 'json':
        # Convert datetime objects to strings for JSON serialization
        data_for_json = result.to_dict()
        # Ensure conversion (redundant if using updated to_dict but safer)
        for item in data_for_json['data']:
            if 'timestamp' in item and isinstance(item['timestamp'], datetime):
                item['timestamp'] = item['timestamp'].isoformat()
        
        # Handle datetime objects in stats
        if 'start_time' in data_for_json['stats'] and isinstance(data_for_json['stats']['start_time'], datetime):
            data_for_json['stats']['start_time'] = data_for_json['stats']['start_time'].isoformat()
        if 'end_time' in data_for_json['stats'] and isinstance(data_for_json['stats']['end_time'], datetime):
            data_for_json['stats']['end_time'] = data_for_json['stats']['end_time'].isoformat()
        
        with open(f"{filename}.json", 'w', encoding='utf-8') as f:
            json.dump(data_for_json, f, indent=2, ensure_ascii=False)
            
    elif format == 'csv':
        with open(f"{filename}.csv", 'w', newline='', encoding='utf-8') as f:
            if result.data:
                fieldnames = ['url', 'title', 'content', 'status_code', 'response_time', 'timestamp']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                
                for item in result.data:
                    writer.writerow({
                        'url': item.url,
                        'title': item.title,
                        'content': item.content[:500],  # Truncate content for CSV
                        'status_code': item.status_code,
                        'response_time': item.response_time,
                        'timestamp': item.timestamp.isoformat() if isinstance(item.timestamp, datetime) else item.timestamp
                    })

# Example usage for testing
async def example_usage():
    """Example of how to use the modular scraper"""
    config = ScrapeConfig(
        max_concurrent=5,
        delay_range=(1, 2),
        auto_save=False,  # Don't auto-save, we'll handle the data ourselves
        extract_links=True
    )
    
    urls = [
        'https://www.ndtv.com/india-news/india-denies-reports-of-elon-musk-joining-donald-trump-pm-narendra-modi-phone-call-on-iran-war-11278482',
        'https://www.ndtv.com/world-news/yemens-houthis-join-iran-war-after-threats-launch-1st-missile-on-israel-11278295',
        'https://www.hindustantimes.com/india-news/pm-modi-speaks-with-saudi-arabia-crown-prince-mohammed-bin-salman-discusses-west-asia-conflict-us-iran-war-101774701521659.html'
    ]
    
    try:
        # Scrape URLs
        result = await scrape_urls(urls, config)
        
        print(f"\nScraped {len(result.data)} pages successfully.")
        print(f"Failed URLs: {len(result.failed_urls)}")
        
        # Print stats
        scraper = AdvancedWebScraper(config)
        scraper.print_stats(result.stats)
        
        # Save results
        save_result_to_file(result, 'example_scrape', 'json')
        
        # Example crawl (uncomment to test)
        # crawl_result = await crawl_website('https://toscrape.com/', max_depth=1, config=config)
        # print(f"Crawled {len(crawl_result.data)} pages successfully.")
        
        return result
        
    except Exception as e:
        logger.error(f"Scraping failed: {e}", exc_info=True)
        return None

if __name__ == "__main__":
    # Clean up previous run files
    for ext in ['.json', '.csv', '.db']:
        p = Path(f"example_scrape{ext}")
        if p.exists():
            p.unlink()
    
    asyncio.run(example_usage())
