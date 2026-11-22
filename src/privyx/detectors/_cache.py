"""LRU detection cache wrapper for entity detectors.

Caches entity detections for previously scanned texts, avoiding redundant regex
or NLP scans across conversation turns where prior turns are re-sent in every request.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict

from privyx.core import Context, Detection, Services
from privyx.detectors._base import Detector


def _key(text: str) -> bytes:
    """The cache key for ``text``: its SHA-256 digest.

    Keyed by the text itself, the cache kept every leaf it had seen alive
    (10,000 leaves of 50 KB held ~500 MB).  SHA-256 rather than BLAKE2b: most
    current CPUs hash it in hardware, about twice as fast.  ``surrogatepass``:
    text parsed from JSON can hold a lone surrogate (``"\\ud800"``), which
    plain UTF-8 refuses.
    """
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()


class CachedDetector(Detector):
    """LRU cache wrapper for entity detectors.

    Caches the :class:`~privyx.core.Detection` of previously scanned texts,
    avoiding redundant regex or NLP detection runs across multi-turn conversations
    (where prior turns are re-sent in every request).

    Args:
        inner: The detector to wrap.
        max_size: Maximum number of entries to keep in the LRU cache (default: 10,000).
    """

    def __init__(self, inner: Detector, max_size: int = 10000) -> None:
        super().__init__()
        self._inner = inner
        self._max_size = max(0, max_size)
        self._cache: OrderedDict[bytes, Detection] = OrderedDict()
        self._hits = 0
        self._misses = 0

    async def open(self, services: Services) -> None:
        await self._inner.open(services)

    async def close(self) -> None:
        await self._inner.close()

    @property
    def inner(self) -> Detector:
        """The wrapped underlying detector."""
        return self._inner

    @property
    def max_size(self) -> int:
        """Maximum number of entries before oldest entries are evicted."""
        return self._max_size

    @property
    def size(self) -> int:
        """Current number of entries in the cache."""
        return len(self._cache)

    @property
    def hits(self) -> int:
        """Number of successful cache lookups."""
        return self._hits

    @property
    def misses(self) -> int:
        """Number of cache lookups that required scanning with the inner detector."""
        return self._misses

    def clear(self) -> None:
        """Empty the cache and reset lookup counters."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    def detect_sync(self, text: str, context: Context) -> Detection:
        """Synchronous detection with LRU cache lookup."""
        key = _key(text)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            self._cache.move_to_end(key)
            return Detection(spans=list(cached.spans))

        self._misses += 1
        if type(self._inner).detect_sync is not Detector.detect_sync:
            detection = self._inner.detect_sync(text, context)
        else:
            detection = asyncio.run(self._inner.detect(text, context))

        if detection.cacheable:
            self._store(key, detection)
        return Detection(spans=list(detection.spans))

    async def detect(self, text: str, context: Context) -> Detection:
        """Asynchronous detection with LRU cache lookup."""
        key = _key(text)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            self._cache.move_to_end(key)
            return Detection(spans=list(cached.spans))

        self._misses += 1
        detection = await self._inner.detect(text, context)
        if detection.cacheable:
            self._store(key, detection)
        return Detection(spans=list(detection.spans))

    def _store(self, key: bytes, detection: Detection) -> None:
        if self._max_size > 0:
            self._cache[key] = detection
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
