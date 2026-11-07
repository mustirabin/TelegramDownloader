"""Telegram user-account client: login flow, private-chat listener, message/link -> catalog items."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events, errors, utils
from telethon.tl import types

from .catalog import Catalog
from .config import SettingsStore
from .downloader import CHUNK, Downloader, FatalError, MediaInfo, accepts
from .links import MessageLink, find_links, parse_link

log = logging.getLogger("tgdl.telegram")

TRANSIENT = (
    ConnectionError, TimeoutError, asyncio.TimeoutError, OSError,
    errors.ServerError, errors.RpcCallFailError, errors.TimedOutError,
)


MIME_EXT = {"video/x-matroska": ".mkv", "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-msvideo": ".avi",
            "video/webm": ".webm", "video/mp2t": ".ts", "video/x-ms-wmv": ".wmv", "video/mpeg": ".mpg"}


def file_ext(name: str | None, mime: str | None) -> str:
    """Prefer the real file name's extension; mimetypes maps video/x-matroska to '.mpv'."""
    suffix = Path(name).suffix.lower() if name else ""
    if suffix and 2 <= len(suffix) <= 6:
        return suffix
    if mime in MIME_EXT:
        return MIME_EXT[mime]
    return (mimetypes.guess_extension(mime or "") or "") if mime else ""


def link_chat_key(link: MessageLink) -> str:
    return f"-100{link.chat}" if link.is_private_channel else f"@{link.chat}"


class TelegramService:
    """Owns the TelethonClient. Also acts as the Downloader's Gateway."""

    def __init__(self, data_dir: Path, store: SettingsStore, catalog: Catalog, downloader: Downloader):
        self.session_path = Path(data_dir) / "tgdl.session"
        self.store = store
        self.catalog = catalog
        self.downloader = downloader
        self.client: TelegramClient | None = None
        self.state = "not_configured"
        self.error: str | None = None
        self.me: dict | None = None
        self._phone: str | None = None
        self._dialogs_loaded = False
        self._lock = asyncio.Lock()
        self.scan_status: dict = {"running": False}

    # ---- status / lifecycle ----------------------------------------------------------------
    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "me": self.me, "phone": self._phone,
                "scan": self.scan_status}

    async def start(self) -> None:
        async with self._lock:
            await self._disconnect()
            s = self.store.settings
            if not s.api_id or not s.api_hash:
                self.state, self.error = "not_configured", None
                return
            try:
                self.client = TelegramClient(str(self.session_path.with_suffix("")), int(s.api_id), s.api_hash,
                                             connection_retries=-1, retry_delay=5, auto_reconnect=True,
                                             request_retries=5, flood_sleep_threshold=60)
                await self.client.connect()
                if await self.client.is_user_authorized():
                    await self._on_authorized()
                else:
                    self.state, self.error = "need_phone", None
            except Exception as e:
                log.exception("telegram start failed")
                self.state, self.error = "error", f"{type(e).__name__}: {e}"

    async def stop(self) -> None:
        async with self._lock:
            await self._disconnect()

    async def _disconnect(self) -> None:
        self.downloader.gateway = None
        self.downloader.suspend_all()
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.me = None
        self._dialogs_loaded = False

    async def _on_authorized(self) -> None:
        me = await self.client.get_me()
        self.me = {"id": me.id, "name": utils.get_display_name(me), "username": me.username, "phone": me.phone}
        self.state, self.error = "ready", None
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        self.downloader.gateway = self
        self.downloader.start()
        for item_id in self.catalog.pending_ids():
            self.downloader.enqueue(item_id)
        log.info("logged in as %s - watching private chats", self.me["name"])
        asyncio.create_task(self._load_dialogs())

    async def _load_dialogs(self) -> None:
        """Fill the entity cache so t.me/c/<id>/... links to joined channels can be resolved."""
        try:
            n = 0
            async for _ in self.client.iter_dialogs():
                n += 1
            self._dialogs_loaded = True
            log.info("loaded %d dialogs", n)
        except Exception as e:
            log.warning("could not load dialogs: %s", e)

    # ---- login -----------------------------------------------------------------------------
    def _need_client(self) -> TelegramClient:
        if self.client is None:
            raise RuntimeError("set api_id and api_hash in Settings first")
        return self.client

    async def send_code(self, phone: str) -> None:
        client = self._need_client()
        self._phone = phone.strip().replace(" ", "")
        await client.send_code_request(self._phone)
        self.state, self.error = "need_code", None

    async def submit_code(self, code: str) -> None:
        client = self._need_client()
        try:
            await client.sign_in(phone=self._phone, code=code.strip())
        except errors.SessionPasswordNeededError:
            self.state, self.error = "need_password", None
            return
        await self._on_authorized()

    async def submit_password(self, password: str) -> None:
        await self._need_client().sign_in(password=password)
        await self._on_authorized()

    async def logout(self) -> None:
        if self.client is not None:
            await self.client.log_out()
        await self.start()

    # ---- incoming messages -----------------------------------------------------------------
    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        try:
            if not event.is_private:
                return
            chat = await event.get_chat()
            if not self.should_watch(event.message, chat):
                return
            n = await self.process_message(event.message, chat)
            if n:
                log.info("new message from %s: %d file(s) queued", utils.get_display_name(chat) or chat.id, n)
        except Exception:
            log.exception("failed handling message")

    def should_watch(self, message: Any, chat: Any) -> bool:
        s = self.store.settings
        is_saved = self.me is not None and getattr(chat, "id", None) == self.me["id"]
        if is_saved:
            if not s.include_saved_messages:
                return False
        elif message.out:
            return False
        if getattr(chat, "bot", False) and not s.include_bots:
            return False
        if s.watch_mode == "allowlist":
            keys = {str(chat.id), (getattr(chat, "username", None) or "").lower(),
                    (utils.get_display_name(chat) or "").casefold()}
            if is_saved:
                keys |= {"me", "saved", "saved messages"}
            wanted = {a.lstrip("@").casefold() for a in s.allowed_chats}
            return bool(keys & wanted)
        return True

    def _message_urls(self, message: Any) -> list[str]:
        urls = []
        for ent, inner in (message.get_entities_text() if message.entities else []):
            if isinstance(ent, types.MessageEntityTextUrl):
                urls.append(ent.url)
            elif isinstance(ent, types.MessageEntityUrl):
                urls.append(inner)
        markup = message.reply_markup
        if isinstance(markup, types.ReplyInlineMarkup):
            for row in markup.rows:
                for b in row.buttons:
                    # older layers: KeyboardButtonUrl.url; newer: KeyboardInlineButton.type = InlineButtonTypeUrl
                    url = getattr(b, "url", None) or getattr(getattr(b, "type", None), "url", None)
                    if isinstance(url, str):
                        urls.append(url)
        return urls

    async def process_message(self, message: Any, chat: Any, auto: bool | None = None) -> int:
        """Create catalog items for a direct attachment and for every t.me link in the message."""
        s = self.store.settings
        auto = s.auto_download if auto is None else auto
        status = "queued" if auto else "new"
        sender = utils.get_display_name(chat) if chat is not None else None
        text = message.message or ""
        created = 0
        info = self.media_info(message)
        if info is not None:
            if accepts(info, s):
                item, new = self.catalog.add(
                    str(utils.get_peer_id(message.peer_id)), message.id, via_chat_id=getattr(chat, "id", None),
                    via_msg_id=message.id, via_sender=sender, original_name=info.name, mime=info.mime,
                    size=info.size, document_id=info.document_id, caption=text[:2000], status=status)
                created += self._maybe_enqueue(item, new)
        for link in find_links(text, self._message_urls(message)):
            item, new = self.catalog.add(
                link_chat_key(link), link.msg_id, via_chat_id=getattr(chat, "id", None), via_msg_id=message.id,
                via_sender=sender, link_url=link.url, caption=text[:2000], status=status)
            created += self._maybe_enqueue(item, new)
        return created

    def _maybe_enqueue(self, item: dict, new: bool) -> int:
        if new and item["status"] == "queued":
            self.downloader.enqueue(item["id"])
        return int(new)

    async def add_link(self, url: str) -> dict:
        link = parse_link(url)
        if link is None:
            raise ValueError("not a Telegram message link (expected https://t.me/c/<id>/<msg> or t.me/<name>/<msg>)")
        item, new = self.catalog.add(link_chat_key(link), link.msg_id, link_url=url, via_sender="manual",
                                     status="queued")
        if not new and item["status"] in ("failed", "cancelled", "skipped", "new"):
            self.downloader.retry(item["id"])
        elif new:
            self.downloader.enqueue(item["id"])
        return self.catalog.get(item["id"])

    async def scan_history(self, per_chat: int) -> None:
        """Backfill: run the last N messages of every watched private chat through process_message."""
        client = self._need_client()
        self.scan_status = {"running": True, "chats": 0, "messages": 0, "found": 0}
        try:
            async for d in client.iter_dialogs():
                if not d.is_user:
                    continue
                self.scan_status["chats"] += 1
                async for m in client.iter_messages(d.entity, limit=per_chat):
                    self.scan_status["messages"] += 1
                    if self.should_watch(m, d.entity):
                        self.scan_status["found"] += await self.process_message(m, d.entity)
            log.info("history scan done: %s", self.scan_status)
        except Exception as e:
            log.exception("history scan failed")
            self.scan_status["error"] = f"{type(e).__name__}: {e}"
        finally:
            self.scan_status["running"] = False

    # ---- Gateway for Downloader ------------------------------------------------------------
    async def _entity(self, file_chat: str):
        if file_chat.startswith("@"):
            return file_chat[1:]
        peer = int(file_chat)
        try:
            return await self.client.get_input_entity(peer)
        except ValueError:
            if not self._dialogs_loaded:
                await self._load_dialogs()
                try:
                    return await self.client.get_input_entity(peer)
                except ValueError:
                    pass
            raise FatalError("chat not found - is your Telegram account a member of that channel/group?")

    async def fetch_message(self, file_chat: str, msg_id: int):
        if self.client is None:
            raise ConnectionError("Telegram not connected")
        entity = await self._entity(file_chat)
        try:
            msg = await self.client.get_messages(entity, ids=msg_id)
        except (errors.ChannelPrivateError, errors.ChannelInvalidError, errors.ChatForbiddenError) as e:
            raise FatalError(f"no access to that channel ({type(e).__name__}) - join it with your account") from e
        except (errors.UsernameInvalidError, errors.UsernameNotOccupiedError) as e:
            raise FatalError(f"unknown channel/user name ({type(e).__name__})") from e
        return msg

    def media_info(self, message: Any) -> MediaInfo | None:
        doc = getattr(message, "document", None)
        if not isinstance(doc, types.Document):
            return None
        if message.gif or message.sticker or message.video_note or message.voice:
            return None
        f = message.file
        is_video = bool((f.mime_type or "").startswith("video/")) or any(
            isinstance(a, types.DocumentAttributeVideo) for a in doc.attributes)
        return MediaInfo(document_id=doc.id, name=f.name, mime=f.mime_type, size=f.size, ext=file_ext(f.name, f.mime_type),
                         is_video=is_video, caption=message.message or "")

    async def iter_download(self, message: Any, offset: int, size: int | None):
        async for chunk in self.client.iter_download(message.document, offset=offset, request_size=CHUNK,
                                                     file_size=size):
            yield chunk

    def classify_error(self, exc: BaseException) -> tuple[str, float]:
        if isinstance(exc, errors.FloodWaitError):
            return "flood", float(exc.seconds)
        if isinstance(exc, (errors.FileReferenceExpiredError, errors.FileReferenceInvalidError)):
            return "expired", 0.0
        if isinstance(exc, (errors.AuthKeyUnregisteredError, errors.UserDeactivatedError)):
            return "fatal", 0.0
        if isinstance(exc, TRANSIENT):
            return "transient", 0.0
        if isinstance(exc, errors.RPCError):
            return "transient" if getattr(exc, "code", 400) >= 500 else "fatal", 0.0
        return "transient", 0.0
