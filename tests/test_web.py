import os

import pytest
from fastapi.testclient import TestClient

from tgdl.web import create_app

H = {"X-TGDL": "1"}


@pytest.fixture
def client(tmp_path):
    api = create_app(tmp_path / "data", start_telegram=False)
    st = api.state.app
    st.store.update({"library_dir": str(tmp_path / "lib")})
    with TestClient(api, base_url="http://127.0.0.1:8765") as c:
        c.st = st
        yield c


def add_done(st, rel, **kw):
    p = st.library / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(kw.pop("content", os.urandom(5000)))
    item, _ = st.catalog.add("-1001", kw.pop("msg", 1), status="done", rel_path=rel, size=p.stat().st_size, **kw)
    return item


def test_security_checks(client):
    assert client.put("/api/settings", json={"max_concurrent_downloads": 3}).status_code == 403  # no header
    assert client.get("/api/status", headers={"host": "evil.example.com"}).status_code == 403  # DNS rebinding
    assert client.put("/api/settings", json={"max_concurrent_downloads": 3}, headers=H).status_code == 200


def test_settings_hide_api_hash(client):
    r = client.put("/api/settings", json={"api_hash": "secret123", "watch_mode": "allowlist",
                                          "allowed_chats": "@a\n123", "video_extensions": "mkv, .MP4"}, headers=H)
    assert r.status_code == 200
    s = client.get("/api/settings").json()
    assert s["api_hash"] == "" and s["api_hash_set"] is True
    assert s["allowed_chats"] == ["@a", "123"] and s["video_extensions"] == [".mkv", ".mp4"]
    client.put("/api/settings", json={"api_hash": ""}, headers=H)  # empty keeps the secret
    assert client.st.store.settings.api_hash == "secret123"
    assert client.put("/api/settings", json={"watch_mode": "bogus"}, headers=H).status_code == 400


def test_library_grouping_stream_edit_delete(client):
    st = client.st
    content = os.urandom(10000)
    e2 = add_done(st, "Series/Fauda/Season 01/Fauda - S01E02.mkv", msg=1, kind="episode", title="Fauda", season=1,
                  episode=2, content=content)
    add_done(st, "Series/Fauda/Season 01/Fauda - S01E01.mkv", msg=2, kind="episode", title="Fauda", season=1, episode=1)
    add_done(st, "Movies/Dune (2021)/Dune (2021).mp4", msg=3, kind="movie", title="Dune", year=2021)
    u = add_done(st, "_Unsorted/clip.mp4", msg=4, kind="unsorted")
    lib = client.get("/api/library").json()
    assert [s["title"] for s in lib["series"]] == ["Fauda"]
    assert [e["episode"] for e in lib["series"][0]["seasons"][0]["episodes"]] == [1, 2]
    assert len(lib["movies"]) == 1 and len(lib["unsorted"]) == 1

    r = client.get(f"/api/items/{e2['id']}/stream", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == content[100:200]

    r = client.patch(f"/api/items/{u['id']}", json={"kind": "episode", "title": "Fauda", "season": 2, "episode": 7},
                     headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["rel_path"] == "Series/Fauda/Season 02/Fauda - S02E07.mp4"
    assert (st.library / "Series/Fauda/Season 02/Fauda - S02E07.mp4").exists()

    assert client.delete(f"/api/items/{e2['id']}", headers=H).status_code == 200
    assert not (st.library / "Series/Fauda/Season 01/Fauda - S01E02.mkv").exists()
    assert client.get(f"/api/items/{e2['id']}/stream").status_code == 404


def test_add_link_requires_login(client):
    assert client.post("/api/downloads", json={"url": "https://t.me/c/1/2"}, headers=H).status_code == 409


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "TelegramDownloader" in r.text
