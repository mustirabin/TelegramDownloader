"""TelegramService message handling with real Telethon message objects (no network)."""

import pytest
from telethon.tl import types
from telethon.tl.patched import Message

from tgdl.telegram import TelegramService, file_ext


def video_msg(msg_id=5, text="", name="Show.S01E02.mkv", mime="video/x-matroska", out=False, entities=None,
              markup=None, attrs=None):
    doc = types.Document(id=77, access_hash=1, file_reference=b"", date=None, mime_type=mime, size=12345, dc_id=2,
                         attributes=attrs if attrs is not None else
                         [types.DocumentAttributeFilename(name), types.DocumentAttributeVideo(duration=10, w=1, h=1)])
    return Message(id=msg_id, peer_id=types.PeerUser(42), date=None, message=text, out=out,
                   media=types.MessageMediaDocument(document=doc), entities=entities, reply_markup=markup)


def text_msg(text, msg_id=6, entities=None, markup=None, out=False):
    return Message(id=msg_id, peer_id=types.PeerUser(42), date=None, message=text, out=out, entities=entities,
                   reply_markup=markup)


class Chat:
    def __init__(self, id=42, username="friend", bot=False, first_name="Dana"):
        self.id, self.username, self.bot, self.first_name, self.last_name = id, username, bot, first_name, None


@pytest.fixture
def svc(env, tmp_path):
    store, cat, gw, dl = env
    s = TelegramService(tmp_path / "data", store, cat, dl)
    s.me = {"id": 1, "name": "Me"}
    return s


def test_file_ext():
    assert file_ext("a.MKV", "video/x-matroska") == ".mkv"
    assert file_ext(None, "video/x-matroska") == ".mkv"
    assert file_ext(None, "video/mp4") == ".mp4"


async def test_direct_video_and_links(svc):
    url = "https://t.me/c/3215507776/11173"
    m = video_msg(text=f"next: {url}", entities=[types.MessageEntityUrl(6, len(url))])
    assert await svc.process_message(m, Chat()) == 2
    items = svc.catalog.list()
    keys = sorted((i["file_chat"], i["file_msg_id"]) for i in items)
    assert keys == [("-1003215507776", 11173), ("42", 5)]
    assert all(i["status"] == "queued" for i in items)
    assert await svc.process_message(m, Chat()) == 0  # same message again: no duplicates


async def test_hidden_link_and_button(svc):
    from telethon import Button
    markup = types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(
        buttons=[Button.url("Episode 4", "https://t.me/c/3215507776/200")])])
    m = text_msg("Episode 3", entities=[types.MessageEntityTextUrl(0, 9, url="https://t.me/c/3215507776/199")],
                 markup=markup)
    assert await svc.process_message(m, Chat()) == 2
    assert sorted(i["file_msg_id"] for i in svc.catalog.list()) == [199, 200]


async def test_gif_and_non_video_ignored(svc):
    gif = video_msg(name="a.mp4", mime="video/mp4", attrs=[types.DocumentAttributeAnimated(),
                                                            types.DocumentAttributeVideo(duration=1, w=1, h=1)])
    assert await svc.process_message(gif, Chat()) == 0
    pdf = video_msg(name="a.pdf", mime="application/pdf", attrs=[types.DocumentAttributeFilename("a.pdf")])
    assert await svc.process_message(pdf, Chat()) == 0


def test_should_watch(svc):
    st = svc.store
    assert svc.should_watch(text_msg("x"), Chat())
    assert not svc.should_watch(text_msg("x", out=True), Chat())  # my own outgoing message to a friend
    assert svc.should_watch(text_msg("x", out=True), Chat(id=1))  # Saved Messages
    st.update({"include_saved_messages": False})
    assert not svc.should_watch(text_msg("x", out=True), Chat(id=1))
    st.update({"include_bots": False})
    assert not svc.should_watch(text_msg("x"), Chat(bot=True))
    st.update({"watch_mode": "allowlist", "allowed_chats": ["@friend"]})
    assert svc.should_watch(text_msg("x"), Chat())
    assert not svc.should_watch(text_msg("x"), Chat(id=7, username="other", first_name="Other"))
    st.update({"allowed_chats": ["7"]})
    assert svc.should_watch(text_msg("x"), Chat(id=7, username=None, first_name="Other"))


async def test_auto_download_off_creates_new_items(svc):
    svc.store.update({"auto_download": False})
    await svc.process_message(text_msg("https://t.me/c/5/6"), Chat())
    assert svc.catalog.list()[0]["status"] == "new"
