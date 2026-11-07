"""Download queue: resumable, retrying, concurrency-limited workers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from .catalog import ACTIVE, Catalog
from .config import SettingsStore
from .naming import Classification, classify, relative_path, resolve_target

log = logging.getLogger("tgdl.download")

CHUNK = 512 * 1024  # Telegram's max request size; resume offsets are aligned to it
MAX_WORKERS = 10
MAX_ATTEMPTS = 8
INCOMPLETE_DIR = ".incomplete"


@dataclass
class MediaInfo:
    document_id: int | None
    name: str | None
    mime: str | None
    size: int | None
    ext: str
    is_video: bool
    caption: str = ""


class Gateway(Protocol):
    async def fetch_message(self, file_chat: str, msg_id: int) -> Any: ...
    def media_info(self, message: Any) -> MediaInfo | None: ...
    def iter_download(self, message: Any, offset: int, size: int | None) -> AsyncIterator[bytes]: ...
    def classify_error(self, exc: BaseException) -> tuple[str, float]:
        """('flood', wait_s) | ('expired', 0) | ('transient', 0) | ('fatal', 0)."""


class FatalError(Exception):
    """Do not retry (no access, no file, ...)."""


class SkipItem(Exception):
    pass


class UserCancelled(Exception):
    pass


@dataclass
class Progress:
    done: int = 0
    size: int | None = None
    speed: float = 0.0
    started: float = field(default_factory=time.monotonic)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    requeue: bool = False
    _last_t: float = field(default_factory=time.monotonic)
    _last_b: int = 0
    _last_log_pct: int = -1

    def tick(self, done: int) -> None:
        self.done = done
        now = time.monotonic()
        dt = now - self._last_t
        if dt >= 1.0:
            inst = (done - self._last_b) / dt
            self.speed = inst if self.speed == 0 else 0.6 * self.speed + 0.4 * inst
            self._last_t, self._last_b = now, done

    def eta(self) -> float | None:
        if self.size and self.speed > 0:
            return max(0.0, (self.size - self.done) / self.speed)
        return None


def accepts(info: MediaInfo, settings) -> bool:
    if settings.download_all_documents:
        return True
    return info.is_video or info.ext.lower() in settings.video_extensions


class Downloader:
    def __init__(self, catalog: Catalog, store: SettingsStore, gateway: Gateway | None = None):
        self.catalog = catalog
        self.store = store
        self.gateway = gateway
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.active: dict[int, Progress] = {}
        self._queued: set[int] = set()
        self._running = 0
        self._slot = asyncio.Condition()
        self._workers: list[asyncio.Task] = []
        self.backoff_base = 2.0

    # ---- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self._workers:
            return
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(MAX_WORKERS)]
        for item_id in self.catalog.pending_ids():
            self.catalog.update(item_id, status="queued")
            self.enqueue(item_id)

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    @property
    def library(self) -> Path:
        return Path(self.store.settings.library_dir)

    def part_path(self, item_id: int) -> Path:
        return self.library / INCOMPLETE_DIR / f"{item_id}.part"

    # ---- public API ------------------------------------------------------------------------
    def enqueue(self, item_id: int) -> None:
        if item_id not in self._queued and item_id not in self.active:
            self._queued.add(item_id)
            self.queue.put_nowait(item_id)

    def cancel(self, item_id: int) -> None:
        prog = self.active.get(item_id)
        if prog:
            prog.cancel.set()
        item = self.catalog.get(item_id)
        if item and item["status"] in (*ACTIVE, "new"):
            self.catalog.update(item_id, status="cancelled", error="cancelled by user")

    def suspend_all(self) -> None:
        """Telegram is disconnecting: stop active downloads but keep them queued (they resume later)."""
        for prog in self.active.values():
            prog.requeue = True
            prog.cancel.set()

    def retry(self, item_id: int) -> None:
        item = self.catalog.get(item_id)
        if item and item["status"] not in ("downloading", "done"):
            self.catalog.update(item_id, status="queued", error=None, attempts=0)
            self.enqueue(item_id)

    def remove_part(self, item_id: int) -> None:
        p = self.part_path(item_id)
        if p.exists():
            p.unlink()

    async def notify_limit_changed(self) -> None:
        async with self._slot:
            self._slot.notify_all()

    # ---- workers ---------------------------------------------------------------------------
    async def _acquire(self) -> None:
        async with self._slot:
            await self._slot.wait_for(lambda: self._running < self.store.settings.max_concurrent_downloads)
            self._running += 1

    async def _release(self) -> None:
        async with self._slot:
            self._running -= 1
            self._slot.notify_all()

    async def _worker(self, n: int) -> None:
        while True:
            item_id = await self.queue.get()
            self._queued.discard(item_id)
            await self._acquire()
            try:
                await self.process(item_id)
            except Exception:  # never let a worker die
                log.exception("unexpected error on item %s", item_id)
                self.catalog.update(item_id, status="failed", error="internal error (see console)")
            finally:
                self.active.pop(item_id, None)
                await self._release()

    async def process(self, item_id: int) -> None:
        item = self.catalog.get(item_id)
        if not item or item["status"] not in ACTIVE:
            return
        gw = self.gateway
        if gw is None:
            return  # stays queued; re-enqueued when Telegram connects
        prog = Progress(size=item["size"])
        self.active[item_id] = prog
        self.catalog.update(item_id, status="downloading", error=None)
        attempt = 0
        while True:
            done_before = prog.done
            try:
                await self._attempt(item_id, prog, gw)
                return
            except UserCancelled:
                if prog.requeue:
                    self.catalog.update(item_id, status="queued", bytes_done=prog.done)
                    return
                self.catalog.update(item_id, status="cancelled", error="cancelled by user", bytes_done=prog.done)
                log.info("#%s cancelled", item_id)
                return
            except SkipItem as e:
                self.catalog.update(item_id, status="skipped", error=str(e))
                log.info("#%s skipped: %s", item_id, e)
                return
            except FatalError as e:
                self.catalog.update(item_id, status="failed", error=str(e))
                log.warning("#%s failed: %s", item_id, e)
                return
            except Exception as e:
                kind, wait = gw.classify_error(e)
                if kind == "fatal":
                    self.catalog.update(item_id, status="failed", error=f"{type(e).__name__}: {e}")
                    log.warning("#%s failed: %s: %s", item_id, type(e).__name__, e)
                    return
                if kind == "flood":
                    log.info("#%s Telegram rate limit, waiting %ss", item_id, int(wait))
                    self.catalog.update(item_id, error=f"rate limited by Telegram, waiting {int(wait)}s")
                    await self._sleep_or_cancel(prog, wait + 1)
                    continue
                if kind == "expired":
                    log.info("#%s file reference expired, refetching message", item_id)
                    continue
                if prog.done > done_before:
                    attempt = 0  # made progress: only count consecutive failures
                attempt += 1
                self.catalog.update(item_id, attempts=attempt, bytes_done=prog.done,
                                    error=f"retry {attempt}/{MAX_ATTEMPTS}: {type(e).__name__}: {e}")
                if attempt >= MAX_ATTEMPTS:
                    self.catalog.update(item_id, status="failed",
                                        error=f"gave up after {attempt} attempts: {type(e).__name__}: {e}")
                    log.warning("#%s gave up: %s", item_id, e)
                    return
                delay = min(60.0, self.backoff_base ** attempt)
                log.info("#%s %s: %s - retry %d/%d in %.0fs", item_id, type(e).__name__, e, attempt, MAX_ATTEMPTS, delay)
                await self._sleep_or_cancel(prog, delay)

    async def _sleep_or_cancel(self, prog: Progress, seconds: float) -> None:
        try:
            await asyncio.wait_for(prog.cancel.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        raise UserCancelled()

    async def _attempt(self, item_id: int, prog: Progress, gw: Gateway) -> None:
        item = self.catalog.get(item_id)
        msg = await gw.fetch_message(item["file_chat"], item["file_msg_id"])
        if msg is None:
            raise FatalError("message not found (deleted, or your account has no access to that chat)")
        info = gw.media_info(msg)
        if info is None:
            raise SkipItem("message has no downloadable file")
        if not accepts(info, self.store.settings):
            raise SkipItem(f"not a video ({info.mime or info.ext or 'unknown type'})")
        caption = info.caption or item["caption"] or ""
        self.catalog.update(item_id, document_id=info.document_id, original_name=info.name, mime=info.mime,
                            size=info.size, caption=caption[:2000])
        prog.size = info.size
        if info.document_id:
            dup = self.catalog.find_done_document(info.document_id, item_id)
            if dup and dup["rel_path"] and (self.library / dup["rel_path"]).exists():
                raise SkipItem(f"already downloaded as #{dup['id']}: {dup['rel_path']}")

        part = self.part_path(item_id)
        part.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        if info.size is not None and have > info.size:
            have = 0
        offset = have - have % CHUNK
        with open(part, "r+b" if part.exists() else "wb") as f:
            f.truncate(offset)
            f.seek(offset)
            done = offset
            prog.done = prog._last_b = done
            if offset:
                log.info("#%s resuming at %.1f MB", item_id, offset / 1e6)
            else:
                log.info("#%s downloading %s (%s)", item_id, info.name or "file", _mb(info.size))
            last_save = time.monotonic()
            if info.size is None or done < info.size:
                async for chunk in gw.iter_download(msg, offset, info.size):
                    if prog.cancel.is_set():
                        raise UserCancelled()
                    f.write(chunk)
                    done += len(chunk)
                    prog.tick(done)
                    if time.monotonic() - last_save > 3:
                        f.flush()
                        self.catalog.update(item_id, bytes_done=done)
                        last_save = time.monotonic()
                    if info.size:
                        pct = int(done * 100 / info.size) // 10 * 10
                        if pct > prog._last_log_pct:
                            prog._last_log_pct = pct
                            log.info("#%s %3d%%  %s / %s  %.1f MB/s", item_id, pct, _mb(done), _mb(info.size),
                                     prog.speed / 1e6)
        if info.size is not None and done != info.size:
            raise ConnectionError(f"incomplete download: got {done} of {info.size} bytes")
        self.finalize(item_id, part, info, caption)

    def finalize(self, item_id: int, part: Path, info: MediaInfo, caption: str) -> None:
        c = classify(info.name, caption)
        self._place(item_id, part, c, info.ext, info.name)
        log.info("#%s saved -> %s", item_id, self.catalog.get(item_id)["rel_path"])

    def _place(self, item_id: int, src: Path, c: Classification, ext: str, original_name: str | None) -> Path:
        rel = relative_path(c, ext, original_name, item_id)
        target = resolve_target(self.library, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, target)
        self.catalog.update(item_id, status="done", error=None, kind=c.kind, title=c.title, season=c.season,
                            episode=c.episode, year=c.year, rel_path=target.relative_to(self.library).as_posix(),
                            bytes_done=target.stat().st_size, completed_at=time.time())
        return target

    def reclassify(self, item_id: int, c: Classification) -> dict:
        item = self.catalog.get(item_id)
        if not item or item["status"] != "done" or not item["rel_path"]:
            raise ValueError("only downloaded items can be moved")
        src = self.library / item["rel_path"]
        if not src.exists():
            raise FileNotFoundError(str(src))
        old_parent = src.parent
        tmp = src.with_name(src.name + ".moving")
        os.replace(src, tmp)
        try:
            self._place(item_id, tmp, c, src.suffix, item["original_name"])
        except Exception:
            os.replace(tmp, src)
            raise
        prune_empty_dirs(old_parent, self.library)
        return self.catalog.get(item_id)


def prune_empty_dirs(start: Path, stop: Path) -> None:
    cur = start
    stop = stop.resolve()
    while cur.resolve() != stop and stop in cur.resolve().parents:
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def _mb(n: int | None) -> str:
    if n is None:
        return "? MB"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"
