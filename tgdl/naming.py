"""Classify a file as episode / movie / unsorted and build its library path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from guessit import guessit

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_GENERIC = re.compile(r"^(video|file|document|movie|img|vid|telegram)?[\s_\-]*\d*[\s_\-]*\d*$", re.I)


def safe_name(name: str, max_len: int = 120) -> str:
    """Windows-safe single path component."""
    s = _INVALID.sub(" ", name)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    if not s:
        s = "untitled"
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s[:max_len].rstrip(". ")


def nice_title(t: str) -> str:
    t = re.sub(r"[._]+", " ", str(t)).strip()
    if t.islower() or t.isupper():
        t = " ".join(w[:1].upper() + w[1:] for w in t.lower().split())
    return t


@dataclass
class Classification:
    kind: str  # "episode" | "movie" | "unsorted"
    title: str | None = None
    season: int | None = None
    episode: int | None = None
    year: int | None = None


def _first(v):
    return v[0] if isinstance(v, list) and v else v


_HEB_SEASON = re.compile(r"(?<![\u0590-\u05FF])(?:עונה|ע['׳]?)\s*(\d{1,2})")
_HEB_EPISODE = re.compile(r"(?<![\u0590-\u05FF])(?:פרק|פ['׳]?)\s*(\d{1,4})")
_SE_LOOSE = re.compile(r"\bS(\d{1,2})[\s._\-]*E(\d{1,4})\b", re.I)


def normalize_hebrew(text: str) -> str:
    """"הבורר עונה 2 פרק 5" -> "הבורר S02E05" so guessit can parse Hebrew season/episode markers."""
    t = _HEB_SEASON.sub(lambda m: f" S{int(m.group(1)):02d}", text)
    if "S" in t and _HEB_EPISODE.search(t):
        t = _HEB_EPISODE.sub(lambda m: f"E{int(m.group(1)):02d}", t)
    elif _HEB_EPISODE.search(t):
        t = _HEB_EPISODE.sub(lambda m: f" S01E{int(m.group(1)):02d}", t)
    return _SE_LOOSE.sub(lambda m: f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}", t)


def _guess(text: str) -> dict:
    try:
        return dict(guessit(normalize_hebrew(text)))
    except Exception:  # guessit can choke on odd input; treat as unknown
        return {}


def _from_guess(g: dict) -> Classification | None:
    title = _first(g.get("title"))
    if not title or _GENERIC.match(str(title)) or len(str(title).strip()) < 2:
        return None
    title = nice_title(title)
    year = _first(g.get("year"))
    if g.get("type") == "episode" and g.get("episode") is not None:
        season = _first(g.get("season"))
        return Classification("episode", title, int(season) if season is not None else 1, int(_first(g["episode"])),
                              int(year) if year else None)
    if g.get("type") == "movie":
        return Classification("movie", title, year=int(year) if year else None)
    return None


def classify(file_name: str | None, caption: str | None = None) -> Classification:
    """Try the file name first, then each non-empty caption line (bots often name files generically)."""
    candidates = []
    if file_name:
        candidates.append(file_name)
    for line in (caption or "").splitlines():
        line = line.strip()
        if line and not line.lower().startswith(("http", "t.me")):
            candidates.append(line)
    fallback = None
    for c in candidates[:6]:
        res = _from_guess(_guess(c))
        if res and res.kind == "episode":
            return res
        if res and fallback is None:
            fallback = res
    return fallback or Classification("unsorted")


def relative_path(c: Classification, ext: str, original_name: str | None, item_id: int) -> PurePosixPath:
    ext = ext.lower() if ext else ".mp4"
    if c.kind == "episode":
        show = safe_name(c.title)
        tag = f"S{c.season:02d}E{c.episode:02d}"
        return PurePosixPath("Series", show, f"Season {c.season:02d}", safe_name(f"{c.title} - {tag}") + ext)
    if c.kind == "movie":
        label = safe_name(f"{c.title} ({c.year})" if c.year else c.title)
        return PurePosixPath("Movies", label, label + ext)
    base = safe_name(Path(original_name).stem) if original_name else f"telegram_{item_id}"
    return PurePosixPath("_Unsorted", base + ext)


def match_existing_dir(parent: Path, name: str) -> str:
    """Reuse an existing folder whose name differs only in case ("breaking bad" vs "Breaking Bad")."""
    if parent.is_dir():
        for child in parent.iterdir():
            if child.is_dir() and child.name.casefold() == name.casefold():
                return child.name
    return name


def resolve_target(library: Path, rel: PurePosixPath) -> Path:
    """Map rel path onto disk, reusing case-variant folders and avoiding overwriting existing files."""
    cur = library
    for part in rel.parts[:-1]:
        cur = cur / match_existing_dir(cur, part)
    target = cur / rel.parts[-1]
    stem, suffix, n = target.stem, target.suffix, 2
    while target.exists():
        target = cur / f"{stem} ({n}){suffix}"
        n += 1
    return target
