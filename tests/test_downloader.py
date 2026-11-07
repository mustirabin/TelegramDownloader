import asyncio
import os

from conftest import Expired, FakeMsg, FloodWait

from tgdl.downloader import CHUNK


def data(n):
    return os.urandom(n)


async def run(dl, item_id):
    await dl.process(item_id)
    return dl.catalog.get(item_id)


async def test_download_and_catalog(env):
    store, cat, gw, dl = env
    payload = data(CHUNK * 3 + 1234)
    gw.add("-1001", 5, FakeMsg(payload))
    item, _ = cat.add("-1001", 5)
    it = await run(dl, item["id"])
    assert it["status"] == "done", it
    assert it["rel_path"] == "Series/Breaking Bad/Season 01/Breaking Bad - S01E02.mkv"
    assert (dl.library / it["rel_path"]).read_bytes() == payload
    assert not dl.part_path(item["id"]).exists()


async def test_resume_from_unaligned_part(env):
    store, cat, gw, dl = env
    payload = data(CHUNK * 4 + 10)
    gw.add("-1001", 6, FakeMsg(payload))
    item, _ = cat.add("-1001", 6)
    part = dl.part_path(item["id"])
    part.parent.mkdir(parents=True)
    part.write_bytes(payload[: CHUNK * 2 + 777])  # interrupted mid-chunk
    it = await run(dl, item["id"])
    assert it["status"] == "done"
    assert gw.offsets == [CHUNK * 2]
    assert (dl.library / it["rel_path"]).read_bytes() == payload


async def test_transient_flood_and_expired_errors_are_retried(env):
    store, cat, gw, dl = env
    payload = data(CHUNK * 5)
    gw.add("-1001", 7, FakeMsg(payload))
    gw.fail_plan = [ConnectionError("reset"), FloodWait(0), Expired(), TimeoutError()]
    item, _ = cat.add("-1001", 7)
    it = await run(dl, item["id"])
    assert it["status"] == "done", it
    assert (dl.library / it["rel_path"]).read_bytes() == payload
    assert gw.offsets[0] == 0 and all(o % CHUNK == 0 for o in gw.offsets) and len(gw.offsets) == 5
    assert gw.offsets == sorted(gw.offsets)  # resumed forward each time, never restarted


async def test_gives_up_after_max_attempts(env, monkeypatch):
    import tgdl.downloader as d
    monkeypatch.setattr(d, "MAX_ATTEMPTS", 3)
    store, cat, gw, dl = env
    gw.add("-1001", 8, FakeMsg(data(CHUNK * 3)))
    gw.fail_plan = [ConnectionError("x")] * 10
    gw.fail_after_chunks = 0  # no progress at all
    item, _ = cat.add("-1001", 8)
    it = await run(dl, item["id"])
    assert it["status"] == "failed" and "gave up after 3" in it["error"]
    assert dl.part_path(item["id"]).exists()  # kept for a later resume


async def test_fatal_and_missing(env):
    store, cat, gw, dl = env
    item, _ = cat.add("-1001", 404)
    assert (await run(dl, item["id"]))["status"] == "failed"
    gw.add("-1001", 9, FakeMsg(data(CHUNK * 2)))
    gw.fail_plan = [PermissionError("no access")]
    item, _ = cat.add("-1001", 9)
    it = await run(dl, item["id"])
    assert it["status"] == "failed" and "no access" in it["error"]


async def test_skip_non_video_and_duplicates(env):
    store, cat, gw, dl = env
    gw.add("-1001", 10, FakeMsg(data(100), name="doc.pdf", mime="application/pdf", is_video=False, doc_id=50))
    item, _ = cat.add("-1001", 10)
    assert (await run(dl, item["id"]))["status"] == "skipped"
    store.update({"download_all_documents": True})
    cat.update(item["id"], status="queued")
    assert (await run(dl, item["id"]))["status"] == "done"

    gw.add("-1001", 11, FakeMsg(data(CHUNK), doc_id=99))
    gw.add("555", 3, FakeMsg(data(CHUNK), doc_id=99))  # same file forwarded elsewhere
    a, _ = cat.add("-1001", 11)
    b, _ = cat.add("555", 3)
    assert (await run(dl, a["id"]))["status"] == "done"
    it = await run(dl, b["id"])
    assert it["status"] == "skipped" and "already downloaded" in it["error"]


async def test_dedup_same_message(env):
    _, cat, _, _ = env
    a, new_a = cat.add("-1001", 1)
    b, new_b = cat.add("-1001", 1)
    assert new_a and not new_b and a["id"] == b["id"]


async def test_cancel_and_concurrency_limit(env):
    store, cat, gw, dl = env
    store.update({"max_concurrent_downloads": 1})
    gw.chunk_delay = 0.05
    for i in (1, 2):
        gw.add("-1001", 100 + i, FakeMsg(data(CHUNK * 20), doc_id=1000 + i))
    a, _ = cat.add("-1001", 101)
    b, _ = cat.add("-1001", 102)
    dl.start()
    dl.enqueue(a["id"])
    dl.enqueue(b["id"])
    await asyncio.sleep(0.3)
    assert list(dl.active) == [a["id"]]  # only one at a time
    dl.cancel(a["id"])
    for _ in range(100):
        if cat.get(b["id"])["status"] == "downloading":
            break
        await asyncio.sleep(0.05)
    assert cat.get(a["id"])["status"] == "cancelled"
    assert cat.get(b["id"])["status"] == "downloading"
    dl.cancel(b["id"])
    await asyncio.sleep(0.2)
    # retry resumes the cancelled one from its .part
    gw.chunk_delay = 0
    gw.offsets.clear()
    dl.retry(a["id"])
    for _ in range(100):
        if cat.get(a["id"])["status"] == "done":
            break
        await asyncio.sleep(0.05)
    assert cat.get(a["id"])["status"] == "done"
    assert gw.offsets[0] > 0
    await dl.stop()


async def test_suspend_requeues(env):
    store, cat, gw, dl = env
    gw.chunk_delay = 0.05
    gw.add("-1001", 200, FakeMsg(data(CHUNK * 20)))
    a, _ = cat.add("-1001", 200)
    task = asyncio.create_task(dl.process(a["id"]))
    await asyncio.sleep(0.2)
    dl.suspend_all()
    await task
    assert cat.get(a["id"])["status"] == "queued"


async def test_reclassify_moves_file(env):
    from tgdl.naming import Classification
    store, cat, gw, dl = env
    gw.add("-1001", 300, FakeMsg(data(CHUNK), name="video_123.mp4", mime="video/mp4"))
    a, _ = cat.add("-1001", 300)
    it = await run(dl, a["id"])
    assert it["rel_path"].startswith("_Unsorted/")
    it = dl.reclassify(a["id"], Classification("episode", "Fauda", 4, 3))
    assert it["rel_path"] == "Series/Fauda/Season 04/Fauda - S04E03.mp4"
    assert (dl.library / it["rel_path"]).exists()
    assert not (dl.library / "_Unsorted").exists()  # empty folder pruned
