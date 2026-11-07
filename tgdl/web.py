"""Local web UI + JSON API (FastAPI). Bound to 127.0.0.1 only."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .catalog import Catalog
from .config import SettingsStore
from .downloader import Downloader, prune_empty_dirs
from .naming import Classification
from .telegram import TelegramService

log = logging.getLogger("tgdl.web")
STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
STREAM_TYPES = {".mkv": "video/webm", ".ts": "video/mp2t", ".m4v": "video/mp4", ".avi": "video/x-msvideo"}


class App:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.store = SettingsStore(self.data_dir)
        self.catalog = Catalog(self.data_dir / "catalog.db")
        self.downloader = Downloader(self.catalog, self.store)
        self.telegram = TelegramService(self.data_dir, self.store, self.catalog, self.downloader)

    @property
    def library(self) -> Path:
        return Path(self.store.settings.library_dir)

    def item_path(self, item: dict) -> Path:
        if not item.get("rel_path"):
            raise HTTPException(404, "file not downloaded")
        lib = self.library.resolve()
        p = (lib / item["rel_path"]).resolve()
        if lib not in p.parents:
            raise HTTPException(400, "bad path")
        return p


def _item_view(app: App, item: dict) -> dict:
    d = dict(item)
    prog = app.downloader.active.get(item["id"])
    if prog:
        d["bytes_done"], d["speed"], d["eta"] = prog.done, prog.speed, prog.eta()
        d["size"] = prog.size or d["size"]
    if item["status"] == "done" and item["rel_path"]:
        d["exists"] = (app.library / item["rel_path"]).exists()
    return d


def create_app(data_dir: Path, start_telegram: bool = True) -> FastAPI:
    app_state = App(data_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app_state.library.mkdir(parents=True, exist_ok=True)
        if start_telegram:
            await app_state.telegram.start()
        yield
        await app_state.downloader.stop()
        await app_state.telegram.stop()
        app_state.catalog.close()

    api = FastAPI(title="TelegramDownloader", lifespan=lifespan, docs_url=None, redoc_url=None)
    api.state.app = app_state

    @api.middleware("http")
    async def local_only(request: Request, call_next):
        # Host check defeats DNS-rebinding; the custom header on writes forces a CORS preflight,
        # so other websites open in your browser cannot drive this API.
        host = (request.url.hostname or "").lower()
        if host not in LOCAL_HOSTS and host != "testserver":
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-tgdl") != "1":
            return JSONResponse({"detail": "missing X-TGDL header"}, status_code=403)
        return await call_next(request)

    def bad(e: Exception):
        raise HTTPException(400, f"{e}")

    # ---- status / settings -----------------------------------------------------------------
    @api.get("/api/status")
    async def status():
        return {"telegram": app_state.telegram.status(), "counts": app_state.catalog.counts(),
                "active": len(app_state.downloader.active), "library_dir": str(app_state.library)}

    @api.get("/api/settings")
    async def get_settings():
        return app_state.store.settings.public_dict()

    @api.put("/api/settings")
    async def put_settings(body: dict):
        try:
            _, changed = app_state.store.update(body)
        except (ValueError, TypeError) as e:
            bad(e)
        if changed & {"api_id", "api_hash"}:
            await app_state.telegram.start()
        if "max_concurrent_downloads" in changed:
            await app_state.downloader.notify_limit_changed()
        if "library_dir" in changed:
            app_state.library.mkdir(parents=True, exist_ok=True)
        return {"changed": sorted(changed), "settings": app_state.store.settings.public_dict()}

    # ---- telegram login --------------------------------------------------------------------
    async def _auth(coro):
        try:
            await coro
        except Exception as e:
            log.warning("login step failed: %s: %s", type(e).__name__, e)
            raise HTTPException(400, f"{type(e).__name__}: {e}")
        return app_state.telegram.status()

    @api.post("/api/auth/phone")
    async def auth_phone(body: dict):
        return await _auth(app_state.telegram.send_code(str(body.get("phone", ""))))

    @api.post("/api/auth/code")
    async def auth_code(body: dict):
        return await _auth(app_state.telegram.submit_code(str(body.get("code", ""))))

    @api.post("/api/auth/password")
    async def auth_password(body: dict):
        return await _auth(app_state.telegram.submit_password(str(body.get("password", ""))))

    @api.post("/api/auth/logout")
    async def auth_logout():
        return await _auth(app_state.telegram.logout())

    @api.post("/api/telegram/reconnect")
    async def reconnect():
        await app_state.telegram.start()
        return app_state.telegram.status()

    # ---- downloads -------------------------------------------------------------------------
    @api.get("/api/downloads")
    async def downloads(filter: str = "all"):
        groups = {"active": ("queued", "downloading", "new"), "failed": ("failed", "cancelled", "skipped"),
                  "done": ("done",)}
        items = app_state.catalog.list(groups.get(filter), limit=500)
        return [_item_view(app_state, i) for i in items]

    @api.post("/api/downloads")
    async def add_download(body: dict):
        if app_state.telegram.state != "ready":
            raise HTTPException(409, "log in to Telegram first (Settings)")
        try:
            item = await app_state.telegram.add_link(str(body.get("url", "")))
        except ValueError as e:
            bad(e)
        return _item_view(app_state, item)

    def _get(item_id: int) -> dict:
        item = app_state.catalog.get(item_id)
        if not item:
            raise HTTPException(404, "no such item")
        return item

    @api.post("/api/downloads/{item_id}/cancel")
    async def cancel(item_id: int):
        _get(item_id)
        app_state.downloader.cancel(item_id)
        return _item_view(app_state, _get(item_id))

    @api.post("/api/downloads/{item_id}/retry")
    async def retry(item_id: int):
        _get(item_id)
        app_state.downloader.retry(item_id)
        return _item_view(app_state, _get(item_id))

    @api.delete("/api/downloads/{item_id}")
    async def remove(item_id: int):
        item = _get(item_id)
        if item["status"] == "done":
            raise HTTPException(400, "item is in the library; delete it from the Library page")
        app_state.downloader.cancel(item_id)
        for _ in range(50):
            if item_id not in app_state.downloader.active:
                break
            await asyncio.sleep(0.1)
        app_state.downloader.remove_part(item_id)
        app_state.catalog.delete(item_id)
        return {"ok": True}

    @api.post("/api/scan")
    async def scan(body: dict):
        if app_state.telegram.state != "ready":
            raise HTTPException(409, "log in to Telegram first (Settings)")
        if app_state.telegram.scan_status.get("running"):
            raise HTTPException(409, "a scan is already running")
        limit = max(1, min(int(body.get("limit", 100)), 5000))
        asyncio.create_task(app_state.telegram.scan_history(limit))
        return {"started": True, "limit": limit}

    # ---- library ---------------------------------------------------------------------------
    @api.get("/api/library")
    async def library():
        series: dict[str, dict] = {}
        movies, unsorted = [], []
        for it in app_state.catalog.list(("done",), limit=100000):
            v = _item_view(app_state, it)
            if it["kind"] == "episode" and it["title"]:
                key = it["title"].casefold()
                s = series.setdefault(key, {"title": it["title"], "seasons": {}})
                s["seasons"].setdefault(it["season"] or 1, []).append(v)
            elif it["kind"] == "movie":
                movies.append(v)
            else:
                unsorted.append(v)
        out = []
        for s in sorted(series.values(), key=lambda x: x["title"].casefold()):
            seasons = [{"season": n, "episodes": sorted(eps, key=lambda e: (e["episode"] or 0, e["id"]))}
                       for n, eps in sorted(s["seasons"].items())]
            out.append({"title": s["title"], "seasons": seasons, "episodes": sum(len(x["episodes"]) for x in seasons)})
        movies.sort(key=lambda m: (m["title"] or "").casefold())
        return {"series": out, "movies": movies, "unsorted": unsorted}

    @api.get("/api/items/{item_id}/stream")
    async def stream(item_id: int, download: bool = False):
        item = _get(item_id)
        p = app_state.item_path(item)
        if not p.exists():
            raise HTTPException(404, "file missing on disk")
        mt = STREAM_TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if download:
            return FileResponse(p, media_type=mt, filename=p.name)
        return FileResponse(p, media_type=mt)

    @api.patch("/api/items/{item_id}")
    async def edit_item(item_id: int, body: dict):
        _get(item_id)
        kind = body.get("kind")
        if kind not in ("episode", "movie", "unsorted"):
            bad(ValueError("kind must be episode, movie or unsorted"))
        title = (body.get("title") or "").strip() or None
        try:
            season = int(body["season"]) if body.get("season") not in (None, "") else None
            episode = int(body["episode"]) if body.get("episode") not in (None, "") else None
            year = int(body["year"]) if body.get("year") not in (None, "") else None
        except ValueError as e:
            bad(e)
        if kind != "unsorted" and not title:
            bad(ValueError("title is required"))
        if kind == "episode" and episode is None:
            bad(ValueError("episode number is required"))
        c = Classification(kind, title, (season or 1) if kind == "episode" else None,
                           episode if kind == "episode" else None, year)
        try:
            item = app_state.downloader.reclassify(item_id, c)
        except (ValueError, FileNotFoundError) as e:
            bad(e)
        return _item_view(app_state, item)

    @api.delete("/api/items/{item_id}")
    async def delete_item(item_id: int, delete_file: bool = True):
        item = _get(item_id)
        if delete_file and item.get("rel_path"):
            p = app_state.item_path(item)
            if p.exists():
                p.unlink()
            prune_empty_dirs(p.parent, app_state.library)
        app_state.catalog.delete(item_id)
        return {"ok": True}

    @api.post("/api/items/{item_id}/open")
    async def open_item(item_id: int, reveal: bool = False):
        p = app_state.item_path(_get(item_id))
        if not p.exists():
            raise HTTPException(404, "file missing on disk")
        _open_on_desktop(p, reveal)
        return {"ok": True}

    @api.post("/api/library/open")
    async def open_library():
        _open_on_desktop(app_state.library, False)
        return {"ok": True}

    api.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return api


def _open_on_desktop(p: Path, reveal: bool) -> None:
    if sys.platform == "win32":
        if reveal:
            subprocess.Popen(["explorer", f"/select,{p}"])
        else:
            os.startfile(str(p))  # noqa: S606 - opens with the default player
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)] if reveal else ["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if reveal else p)])
