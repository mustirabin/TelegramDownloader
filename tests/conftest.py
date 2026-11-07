import asyncio

import pytest

from tgdl.catalog import Catalog
from tgdl.config import SettingsStore
from tgdl.downloader import Downloader, MediaInfo


class FakeMsg:
    def __init__(self, data: bytes, name="Breaking.Bad.S01E02.720p.mkv", mime="video/x-matroska", doc_id=1,
                 caption="", is_video=True):
        self.data, self.name, self.mime, self.doc_id, self.caption, self.is_video = data, name, mime, doc_id, caption, is_video


class FakeGateway:
    """Stands in for TelegramService: serves FakeMsg contents in CHUNK pieces, can inject failures."""

    def __init__(self):
        self.messages: dict[tuple[str, int], FakeMsg] = {}
        self.fail_plan: list[BaseException] = []  # raised (once each) mid-download
        self.fail_after_chunks = 1
        self.offsets: list[int] = []
        self.chunk_delay = 0.0

    def add(self, chat: str, msg_id: int, msg: FakeMsg):
        self.messages[(chat, msg_id)] = msg

    async def fetch_message(self, chat, msg_id):
        return self.messages.get((chat, msg_id))

    def media_info(self, m):
        if m.data is None:
            return None
        ext = "." + m.name.rsplit(".", 1)[-1] if m.name and "." in m.name else ".mp4"
        return MediaInfo(m.doc_id, m.name, m.mime, len(m.data), ext, m.is_video, m.caption)

    async def iter_download(self, m, offset, size):
        from tgdl.downloader import CHUNK
        assert offset % CHUNK == 0
        self.offsets.append(offset)
        sent = 0
        for i in range(offset, len(m.data), CHUNK):
            if self.fail_plan and sent >= self.fail_after_chunks:
                raise self.fail_plan.pop(0)
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            yield m.data[i:i + CHUNK]
            sent += 1

    def classify_error(self, exc):
        if isinstance(exc, FloodWait):
            return "flood", exc.seconds
        if isinstance(exc, Expired):
            return "expired", 0
        if isinstance(exc, PermissionError):
            return "fatal", 0
        return "transient", 0


class FloodWait(Exception):
    def __init__(self, seconds):
        super().__init__(f"wait {seconds}")
        self.seconds = seconds


class Expired(Exception):
    pass


@pytest.fixture
def env(tmp_path):
    store = SettingsStore(tmp_path / "data")
    store.update({"library_dir": str(tmp_path / "lib")})
    cat = Catalog(tmp_path / "data" / "catalog.db")
    gw = FakeGateway()
    dl = Downloader(cat, store, gw)
    dl.backoff_base = 0.01
    yield store, cat, gw, dl
    cat.close()
