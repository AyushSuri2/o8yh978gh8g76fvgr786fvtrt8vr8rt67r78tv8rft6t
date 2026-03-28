"""
DuckDuckGo Search Module – 100% feature-complete for the `ddgs` library
pip install -U ddgs
"""

from typing import List, Dict, Optional, Generator, Any, Union
from dataclasses import dataclass, asdict
from urllib.parse import urlparse

# Gracefully handle imports without killing the host process!
try:
    from ddgs import DDGS
except ImportError:
    raise ImportError("Please install the ddgs package: pip install -U ddgs")

try:
    from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
except ImportError:
    # If the library authors move or remove these exceptions, fallback gracefully
    class DDGSException(Exception): pass
    class RatelimitException(Exception): pass
    class TimeoutException(Exception): pass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    position: int
    published_date: Optional[str] = None
    source: Optional[str] = None

    def __dict__(self):  # handy for JSON serialisation
        return asdict(self)


class DDGSearcher:
    """
    Feature-complete DuckDuckGo searcher using the official `ddgs` library.
    Supports proxy, timeout, pagination, and all backend-specific filters.
    """

    # Class-level default for threads (parallel requests)
    threads: int = 20

    def __init__(
        self,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        time_range: Optional[str] = None,
        backend: str = "auto",
        proxy: Optional[str] = None,
        timeout: int = 5,
        delay: float = 1.0,
    ):
        self.region = region
        self.safesearch = safesearch
        self.time_range = time_range
        self.backend = backend
        self.proxy = proxy
        self.timeout = timeout
        self.delay = delay

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _get_params(self, **overrides: Any) -> Dict[str, Any]:
        """Build parameter dict, cleaning out None values and backend if needed."""
        params = {
            "region": self.region,
            "safesearch": self.safesearch,
            "timelimit": self.time_range,
            "backend": self.backend,
        }
        params.update({k: v for k, v in overrides.items() if v is not None})
        if overrides.get("drop_backend"):
            params.pop("backend", None)
        return params

    def _get_ddgs_instance(self) -> DDGS:
        """Create a properly configured DDGS instance."""
        return DDGS(proxy=self.proxy, timeout=self.timeout)

    # ------------------------------------------------------------------
    # TEXT SEARCH
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        max_results: Optional[int] = 30,
        region: Optional[str] = None,
        safesearch: Optional[str] = None,
        time_range: Optional[str] = None,
        backend: Optional[Union[str, List[str]]] = None,
        page: int = 1,
        raw: bool = False,
    ) -> Union[List[SearchResult], List[Dict[str, Any]]]:
        """
        Perform a text search. Returns parsed SearchResult objects unless raw=True.
        Backend can be a string ("google,brave") or a list ["google", "brave"].
        """
        if not query.strip():
            return []

        # Normalize backend to comma-separated string
        if isinstance(backend, list):
            backend = ", ".join(backend)

        effective = self._get_params(
            region=region, safesearch=safesearch, timelimit=time_range, backend=backend
        )
        print(f"DDGSearcher: text search  '{query}'  | params: {effective}")

        try:
            with self._get_ddgs_instance() as ddgs:
                ddgs.threads = self.threads
                raw_results = ddgs.text(
                    query, max_results=max_results, page=page, **effective
                )
                if raw:
                    return list(raw_results)
                return [
                    SearchResult(
                        title=r.get("title", ""),
                        url=r.get("href", ""),
                        snippet=r.get("body", ""),
                        position=i,
                    )
                    for i, r in enumerate(raw_results, 1)
                ]
        except (RatelimitException, TimeoutException, DDGSException) as exc:
            print(f"DDGSearcher: text search failed: {exc.__class__.__name__}: {exc}")
            return []

    # ------------------------------------------------------------------
    # NEWS
    # ------------------------------------------------------------------
    def search_news(
        self,
        query: str,
        max_results: Optional[int] = 20,
        region: Optional[str] = None,
        safesearch: Optional[str] = None,
        time_range: Optional[str] = None,
        backend: Optional[Union[str, List[str]]] = None,
        page: int = 1,
        raw: bool = False,
    ) -> Union[List[SearchResult], List[Dict[str, Any]]]:
        """Search news. Backend can be 'bing', 'duckduckgo', or 'yahoo'."""
        if not query.strip():
            return []

        if isinstance(backend, list):
            backend = ", ".join(backend)

        effective = self._get_params(
            region=region,
            safesearch=safesearch,
            timelimit=time_range,
            backend=backend,
            drop_backend=True,
        )
        print(f"DDGSearcher: news search  '{query}'  | params: {effective}")

        try:
            with self._get_ddgs_instance() as ddgs:
                ddgs.threads = self.threads
                raw_results = ddgs.news(
                    query, max_results=max_results, page=page, **effective
                )
                if raw:
                    return list(raw_results)
                return [
                    SearchResult(
                        title=r.get("title", ""),
                        url=r.get("url", ""),
                        snippet=r.get("body", ""),
                        position=i,
                        published_date=r.get("date"),
                        source=r.get("source"),
                    )
                    for i, r in enumerate(raw_results, 1)
                ]
        except (RatelimitException, TimeoutException, DDGSException) as exc:
            print(f"DDGSearcher: news search failed: {exc.__class__.__name__}: {exc}")
            return []

    # ------------------------------------------------------------------
    # IMAGES (with all filters)
    # ------------------------------------------------------------------
    def search_images(
        self,
        query: str,
        max_results: Optional[int] = 30,
        region: Optional[str] = None,
        safesearch: Optional[str] = None,
        time_range: Optional[str] = None,
        size: Optional[str] = None,
        color: Optional[str] = None,
        type_image: Optional[str] = None,
        layout: Optional[str] = None,
        license_image: Optional[str] = None,
        page: int = 1,
        raw: bool = False,
    ) -> List[Dict[str, Any]]:
        """Search images with full filter support."""
        if not query.strip():
            return []

        effective = self._get_params(
            region=region,
            safesearch=safesearch,
            timelimit=time_range,
            drop_backend=True,
        )
        # Add image-specific filters
        for key, val in {
            "size": size,
            "color": color,
            "type_image": type_image,
            "layout": layout,
            "license_image": license_image,
        }.items():
            if val is not None:
                effective[key] = val

        print(f"DDGSearcher: image search  '{query}'  | params: {effective}")

        try:
            with self._get_ddgs_instance() as ddgs:
                ddgs.threads = self.threads
                results = ddgs.images(query, max_results=max_results, page=page, **effective)
                return list(results) if raw else list(results)
        except (RatelimitException, TimeoutException, DDGSException) as exc:
            print(f"DDGSearcher: image search failed: {exc.__class__.__name__}: {exc}")
            return []

    # ------------------------------------------------------------------
    # VIDEOS (with all filters)
    # ------------------------------------------------------------------
    def search_videos(
        self,
        query: str,
        max_results: Optional[int] = 30,
        region: Optional[str] = None,
        safesearch: Optional[str] = None,
        time_range: Optional[str] = None,
        resolution: Optional[str] = None,
        duration: Optional[str] = None,
        license_videos: Optional[str] = None,
        page: int = 1,
        raw: bool = False,
    ) -> List[Dict[str, Any]]:
        """Search videos with full filter support."""
        if not query.strip():
            return []

        effective = self._get_params(
            region=region,
            safesearch=safesearch,
            timelimit=time_range,
            drop_backend=True,
        )
        # Add video-specific filters
        for key, val in {
            "resolution": resolution,
            "duration": duration,
            "license_videos": license_videos,
        }.items():
            if val is not None:
                effective[key] = val

        print(f"DDGSearcher: video search  '{query}'  | params: {effective}")

        try:
            with self._get_ddgs_instance() as ddgs:
                ddgs.threads = self.threads
                results = ddgs.videos(query, max_results=max_results, page=page, **effective)
                return list(results) if raw else list(results)
        except (RatelimitException, TimeoutException, DDGSException) as exc:
            print(f"DDGSearcher: video search failed: {exc.__class__.__name__}: {exc}")
            return []

    # ------------------------------------------------------------------
    # BOOKS (newly added)
    # ------------------------------------------------------------------
    def search_books(
        self,
        query: str,
        max_results: Optional[int] = 20,
        page: int = 1,
        raw: bool = False,
    ) -> List[Dict[str, Any]]:
        """Search books (uses annasarchive backend)."""
        if not query.strip():
            return []

        print(f"DDGSearcher: books search  '{query}'  | max_results={max_results}, page={page}")

        try:
            with self._get_ddgs_instance() as ddgs:
                ddgs.threads = self.threads
                results = ddgs.books(query, max_results=max_results, page=page)
                return list(results) if raw else list(results)
        except (RatelimitException, TimeoutException, DDGSException) as exc:
            print(f"DDGSearcher: books search failed: {exc.__class__.__name__}: {exc}")
            return []

    # ------------------------------------------------------------------
    # ITERATOR (lazy text search)
    # ------------------------------------------------------------------
    def search_iterator(
        self,
        query: str,
        max_results: int = 100,
        region: Optional[str] = None,
        safesearch: Optional[str] = None,
        time_range: Optional[str] = None,
        backend: Optional[Union[str, List[str]]] = None,
        page: int = 1,
    ) -> Generator[SearchResult, None, None]:
        """Lazy generator for text search results."""
        if not query.strip():
            return

        if isinstance(backend, list):
            backend = ", ".join(backend)

        effective = self._get_params(
            region=region,
            safesearch=safesearch,
            timelimit=time_range,
            backend=backend,
        )
        print(f"DDGSearcher: iterator search  '{query}'  | params: {effective}")

        try:
            with self._get_ddgs_instance() as ddgs:
                ddgs.threads = self.threads
                raw = ddgs.text(query, max_results=max_results, page=page, **effective)
                for i, r in enumerate(raw, 1):
                    yield SearchResult(
                        title=r.get("title", ""),
                        url=r.get("href", ""),
                        snippet=r.get("body", ""),
                        position=i,
                    )
        except (RatelimitException, TimeoutException, DDGSException) as exc:
            print(f"DDGSearcher: iterator failed: {exc.__class__.__name__}: {exc}")

    # ------------------------------------------------------------------
    # UTILITIES
    # ------------------------------------------------------------------
    def get_search_summary(self, results: List[SearchResult]) -> Dict[str, int]:
        """Return summary stats for parsed results."""
        if not results:
            return {"total_results": 0, "unique_domains": 0}
        domains = {urlparse(r.url).netloc for r in results if r.url}
        return {"total_results": len(results), "unique_domains": len(domains)}

    @staticmethod
    def get_available_backends(search_type: str) -> List[str]:
        """Return list of available backends for a search type."""
        backends = {
            "text": ["bing", "brave", "duckduckgo", "google", "mojeek", "yandex", "yahoo", "wikipedia"],
            "images": ["duckduckgo"],
            "videos": ["duckduckgo"],
            "news": ["bing", "duckduckgo", "yahoo"],
            "books": ["annasarchive"],
        }
        return backends.get(search_type, [])

    @staticmethod
    def get_image_filters() -> Dict[str, List[str]]:
        """Return available image search filters."""
        return {
            "size": ["Small", "Medium", "Large", "Wallpaper"],
            "color": ["color", "Monochrome", "Red", "Orange", "Yellow", "Green", "Blue", "Purple", "Pink", "Brown", "Black", "Gray", "Teal", "White"],
            "type_image": ["photo", "clipart", "gif", "transparent", "line"],
            "layout": ["Square", "Tall", "Wide"],
            "license_image": ["any", "Public", "Share", "ShareCommercially", "Modify", "ModifyCommercially"],
        }

    @staticmethod
    def get_video_filters() -> Dict[str, List[str]]:
        """Return available video search filters."""
        return {
            "resolution": ["high", "standard"],
            "duration": ["short", "medium", "long"],
            "license_videos": ["creativeCommon", "youtube"],
        }


# ------------------------------------------------------------------
# Convenience one-liners for every search type
# ------------------------------------------------------------------
def search_ddg(query: str, max_results: int = 10, **kw) -> List[SearchResult]:
    """Quick text search."""
    s = DDGSearcher(**{k: v for k, v in kw.items() if k in DDGSearcher.__init__.__code__.co_varnames})
    text_kw = {k: v for k, v in kw.items() if k not in DDGSearcher.__init__.__code__.co_varnames}
    return s.search(query, max_results=max_results, **text_kw)


def search_ddg_news(query: str, max_results: int = 10, **kw) -> List[SearchResult]:
    """Quick news search."""
    s = DDGSearcher(**{k: v for k, v in kw.items() if k in DDGSearcher.__init__.__code__.co_varnames})
    news_kw = {k: v for k, v in kw.items() if k not in DDGSearcher.__init__.__code__.co_varnames}
    return s.search_news(query, max_results=max_results, **news_kw)


def search_ddg_images(query: str, max_results: int = 10, **kw) -> List[Dict[str, Any]]:
    """Quick image search."""
    s = DDGSearcher(**{k: v for k, v in kw.items() if k in DDGSearcher.__init__.__code__.co_varnames})
    img_kw = {k: v for k, v in kw.items() if k not in DDGSearcher.__init__.__code__.co_varnames}
    return s.search_images(query, max_results=max_results, **img_kw)


def search_ddg_videos(query: str, max_results: int = 10, **kw) -> List[Dict[str, Any]]:
    """Quick video search."""
    s = DDGSearcher(**{k: v for k, v in kw.items() if k in DDGSearcher.__init__.__code__.co_varnames})
    vid_kw = {k: v for k, v in kw.items() if k not in DDGSearcher.__init__.__code__.co_varnames}
    return s.search_videos(query, max_results=max_results, **vid_kw)


def search_ddg_books(query: str, max_results: int = 10, **kw) -> List[Dict[str, Any]]:
    """Quick books search."""
    s = DDGSearcher(**{k: v for k, v in kw.items() if k in DDGSearcher.__init__.__code__.co_varnames})
    book_kw = {k: v for k, v in kw.items() if k not in DDGSearcher.__init__.__code__.co_varnames}
    return s.search_books(query, max_results=max_results, **book_kw)


def search_ddg_iterator(query: str, max_results: int = 100, **kw) -> Generator[SearchResult, None, None]:
    """Quick lazy iterator."""
    s = DDGSearcher(**{k: v for k, v in kw.items() if k in DDGSearcher.__init__.__code__.co_varnames})
    iter_kw = {k: v for k, v in kw.items() if k not in DDGSearcher.__init__.__code__.co_varnames}
    yield from s.search_iterator(query, max_results=max_results, **iter_kw)


# ------------------------------------------------------------------
# Comprehensive self-test
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("=== DuckDuckGo Search Module (ddgs) – comprehensive test ===\n")

    s = DDGSearcher(proxy=None, timeout=10, backend="auto")

    print("1. Text search (2 results):")
    for r in s.search("python programming", max_results=2):
        print(f"  - {r.title[:60]}…  {r.url}")

    print("\n2. News search (1 result):")
    for r in s.search_news("technology", max_results=1, time_range="w"):
        print(f"  - {r.title[:60]}…  {r.source}")

    print("\n3. Image search (1 result, Monochrome):")
    for img in s.search_images("cute kitten", max_results=1, color="Monochrome"):
        print(f"  - {img.get('title', 'N/A')[:60]}…  {img.get('image')}")

    print("\n4. Video search (1 result, high res):")
    for vid in s.search_videos("funny dog", max_results=1, resolution="high"):
        print(f"  - {vid.get('title', 'N/A')[:60]}…  {vid.get('content')}")

    print("\n5. Books search (1 result):")
    for book in s.search_books("sea wolf", max_results=1):
        print(f"  - {book.get('title', 'N/A')} by {book.get('author', 'N/A')}")

    print("\n6. Iterator search (3 results):")
    for i, r in enumerate(s.search_iterator("data science", max_results=3)):
        print(f"  {i+1}. {r.title[:60]}…")

    print("\n7. Available backends for 'text':", s.get_available_backends("text"))
    print("8. Image filters:", list(s.get_image_filters().keys()))
    print("9. Video filters:", list(s.get_video_filters().keys()))

    print("\n=== Test complete ===")

import sys

if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "python programming"

    print(f"\nRunning search for: {query}\n")

    s = DDGSearcher()

    results = s.search(query, max_results=5)

    for r in results:
        print(f"{r.title} -> {r.url}")
