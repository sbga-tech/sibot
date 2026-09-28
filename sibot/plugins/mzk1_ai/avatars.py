"""Bounded, file-cached avatar downloads for rendered rankings."""

import asyncio
import hashlib
import io
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import httpx
from nonebot import logger
from PIL import Image, UnidentifiedImageError

AVATAR_PIXELS = 96
_CACHE_SECONDS = 24 * 60 * 60
_FAILURE_RETRY_SECONDS = 10 * 60
_MAX_BYTES = 1024 * 1024
_CONCURRENCY = 4


class AvatarCache:
    """Serve square RGBA avatars; failures fall back to None, never raise."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._failures: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None

    async def load(self, urls: Sequence[str]) -> list[Image.Image | None]:
        semaphore = asyncio.Semaphore(_CONCURRENCY)
        return list(await asyncio.gather(*(self._load(url, semaphore) for url in urls)))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _load(self, url: str, semaphore: asyncio.Semaphore) -> Image.Image | None:
        if not url.startswith("https://"):
            return None
        path = self._directory / f"{hashlib.sha256(url.encode()).hexdigest()}.png"
        cached = await asyncio.to_thread(_read_cached, path)
        if cached is not None:
            return cached
        if time.monotonic() - self._failures.get(url, -_FAILURE_RETRY_SECONDS) < (
            _FAILURE_RETRY_SECONDS
        ):
            return None
        async with semaphore:
            payload = await self._download(url)
        if payload is None:
            self._failures[url] = time.monotonic()
            return None
        image = await asyncio.to_thread(_store, path, payload)
        if image is None:
            self._failures[url] = time.monotonic()
        return image

    async def _download(self, url: str) -> bytes | None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(3.0), follow_redirects=True
            )
        try:
            # GitHub serves a resized image when asked for a size.
            async with self._client.stream(
                "GET", url, params={"s": str(AVATAR_PIXELS)}
            ) as response:
                if not response.is_success:
                    return None
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload += chunk
                    if len(payload) > _MAX_BYTES:
                        return None
                return bytes(payload)
        except httpx.HTTPError as error:
            logger.warning("Mzk1 AI avatar download failed: {}", type(error).__name__)
            return None


def _read_cached(path: Path) -> Image.Image | None:
    try:
        if time.time() - path.stat().st_mtime > _CACHE_SECONDS:
            return None
        with Image.open(path) as image:
            return image.convert("RGBA")
    except (OSError, UnidentifiedImageError):
        return None


def _store(path: Path, payload: bytes) -> Image.Image | None:
    try:
        with Image.open(io.BytesIO(payload)) as source:
            image = source.convert("RGBA")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
    image = image.resize((AVATAR_PIXELS, AVATAR_PIXELS), Image.Resampling.LANCZOS)
    # Concurrent misses for one URL each write their own file; the cache is
    # best-effort, so a write failure still returns the decoded image.
    temporary = path.with_name(f".{path.stem}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(temporary, format="PNG")
        temporary.replace(path)
    except OSError as error:
        logger.warning("Mzk1 AI avatar cache write failed: {}", type(error).__name__)
    finally:
        temporary.unlink(missing_ok=True)
    return image
