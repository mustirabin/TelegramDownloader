from pathlib import Path

from tgdl.links import find_links, parse_link
from tgdl.naming import classify, relative_path, resolve_target, safe_name


def test_private_channel_link():
    l = parse_link("https://t.me/c/3215507776/11173")
    assert (l.chat, l.msg_id, l.is_private_channel) == (3215507776, 11173, True)


def test_topic_and_public_links():
    assert parse_link("https://t.me/c/123/4/99").msg_id == 99
    l = parse_link("t.me/SomeChannel/55?single")
    assert (l.chat, l.msg_id) == ("SomeChannel", 55)
    assert parse_link("https://t.me/joinchat/abcdef") is None
    assert parse_link("https://t.me/somechannel") is None
    assert parse_link("https://example.com/c/1/2") is None


def test_find_links_dedup_and_punctuation():
    text = "ep1: https://t.me/c/3215507776/11173, again https://t.me/c/3215507776/11173. and t.me/c/3215507776/11174!"
    assert [l.msg_id for l in find_links(text)] == [11173, 11174]
    assert [l.msg_id for l in find_links("", ["https://t.me/c/1/2"])] == [2]


def test_classify_english():
    c = classify("Breaking.Bad.S01E02.720p.mkv")
    assert (c.kind, c.title, c.season, c.episode) == ("episode", "Breaking Bad", 1, 2)
    c = classify("Inception.2010.1080p.BluRay.mkv")
    assert (c.kind, c.title, c.year) == ("movie", "Inception", 2010)
    assert classify("Friends - 1x05.avi").episode == 5


def test_classify_hebrew():
    c = classify("הבורר עונה 2 פרק 5.mp4")
    assert (c.kind, c.title, c.season, c.episode) == ("episode", "הבורר", 2, 5)
    c = classify("קופה ראשית - עונה 1 - פרק 12.mp4")
    assert (c.title, c.season, c.episode) == ("קופה ראשית", 1, 12)
    c = classify("שעת נעילה פרק 3.mp4")
    assert (c.kind, c.season, c.episode) == ("episode", 1, 3)


def test_generic_filename_falls_back_to_caption():
    c = classify("video_2023-01-01_12-00-00.mp4", "The Office S03E04\nhttps://t.me/x/1")
    assert (c.kind, c.title, c.season, c.episode) == ("episode", "The Office", 3, 4)
    assert classify("x.mp4").kind == "unsorted"


def test_paths_and_windows_safety(tmp_path):
    c = classify("Breaking.Bad.S01E02.mkv")
    assert relative_path(c, ".mkv", None, 1).as_posix() == "Series/Breaking Bad/Season 01/Breaking Bad - S01E02.mkv"
    m = classify("The.Godfather.1972.mkv")
    assert relative_path(m, ".mkv", None, 1).as_posix() == "Movies/The Godfather (1972)/The Godfather (1972).mkv"
    assert safe_name('a<b>:c"d/e\\f|g?h*') == "a b c d e f g h"
    assert safe_name("CON") == "_CON"
    assert safe_name("name. ") == "name"


def test_resolve_target_reuses_case_variant_and_avoids_overwrite(tmp_path):
    (tmp_path / "Series" / "breaking bad" / "Season 01").mkdir(parents=True)
    rel = relative_path(classify("Breaking.Bad.S01E02.mkv"), ".mkv", None, 1)
    t = resolve_target(tmp_path, rel)
    assert t.parent == tmp_path / "Series" / "breaking bad" / "Season 01"
    t.write_bytes(b"x")
    assert resolve_target(tmp_path, rel).name == "Breaking Bad - S01E02 (2).mkv"
