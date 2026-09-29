"""LRU detection cache wrapper for entity detectors.

Caches entity detections for previously scanned texts, avoiding redundant regex
or NLP scans across conversation turns where prior turns are re-sent in every request.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict

from privyx.core.context import Context
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector, Detector


def _key(text: str) -> bytes:
    """The cache key for ``text``: its SHA-256 digest.

    Keyed by the text itself, the cache kept every leaf it had seen alive
    (10,000 leaves of 50 KB held ~500 MB).  SHA-256 rather than BLAKE2b: most
    current CPUs hash it in hardware, about twice as fast.  ``surrogatepass``:
    text parsed from JSON can hold a lone surrogate (``"\\ud800"``), which
    plain UTF-8 refuses.
    """
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()


class CachedDetector(BaseDetector):
    """LRU cache wrapper for entity detectors.

    Caches the :class:`~privyx.core.result.Detection` of previously scanned texts,
    avoiding redundant regex or NLP detection runs across multi-turn conversations
    (where prior turns are re-sent in every request).

    Args:
        inner: The underlying :class:`~privyx.privacy.detector.base.Detector` to wrap.
        max_size: Maximum number of entries to keep in the LRU cache (default: 10,000).
    """

    def __init__(self, inner: Detector, max_size: int = 10000) -> None:
        super().__init__()
        self._inner = inner
        self.name = inner.name
        self._max_size = max(0, max_size)
        self._cache: OrderedDict[bytes, Detection] = OrderedDict()
        self._hits = 0
        self._misses = 0

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

    def unwrap(self) -> Detector:
        """Return the innermost non-cached detector."""
        detector: Detector = self._inner
        while isinstance(detector, CachedDetector):
            detector = detector.inner
        return detector

    def detect_sync(self, text: str, context: Context) -> Detection:
        """Synchronous detection with LRU cache lookup."""
        key = _key(text)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            self._cache.move_to_end(key)
            return Detection(spans=list(cached.spans))

        self._misses += 1
        if hasattr(self._inner, "detect_sync"):
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
