"""Settings persisted as JSON in the data directory (editable from the web UI)."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_VIDEO_EXTS = [
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm", ".flv", ".mpg", ".mpeg",
]


def default_library_dir() -> str:
    return str(Path.home() / "Videos" / "TelegramLibrary")


@dataclass
class Settings:
    api_id: int | None = None
    api_hash: str = ""
    library_dir: str = field(default_factory=default_library_dir)
    max_concurrent_downloads: int = 2
    auto_download: bool = True
    # "all_private": every private chat; "allowlist": only chats listed in allowed_chats
    watch_mode: str = "all_private"
    allowed_chats: list[str] = field(default_factory=list)
    include_saved_messages: bool = True
    include_bots: bool = True
    video_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_VIDEO_EXTS))
    download_all_documents: bool = False
    ui_port: int = 8765

    def public_dict(self) -> dict:
        d = asdict(self)
        d["api_hash"] = ""
        d["api_hash_set"] = bool(self.api_hash)
        return d


class SettingsStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "settings.json"
        self._lock = threading.Lock()
        self.settings = self._load()

    def _load(self) -> Settings:
        s = Settings()
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(Settings)}
            for k, v in raw.items():
                if k in known:
                    setattr(s, k, v)
        # env vars win, so keys never have to be written to disk if he prefers
        if os.environ.get("TG_API_ID"):
            s.api_id = int(os.environ["TG_API_ID"])
        if os.environ.get("TG_API_HASH"):
            s.api_hash = os.environ["TG_API_HASH"]
        return s

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.settings), indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    def update(self, changes: dict) -> tuple[Settings, set[str]]:
        """Apply validated changes; returns (settings, names of fields that changed)."""
        s = self.settings
        changed: set[str] = set()
        for k, v in changes.items():
            if k in ("api_hash",) and not v:
                continue  # empty = keep existing secret
            if k == "api_id":
                v = int(v) if v not in (None, "") else None
            elif k in ("max_concurrent_downloads", "ui_port"):
                v = int(v)
                if k == "max_concurrent_downloads" and not 1 <= v <= 10:
                    raise ValueError("max_concurrent_downloads must be 1-10")
            elif k == "watch_mode":
                if v not in ("all_private", "allowlist"):
                    raise ValueError("watch_mode must be all_private or allowlist")
            elif k in ("allowed_chats", "video_extensions"):
                if isinstance(v, str):
                    v = [x.strip() for x in v.replace(",", "\n").splitlines()]
                v = [x for x in (str(i).strip() for i in v) if x]
                if k == "video_extensions":
                    v = [x.lower() if x.startswith(".") else "." + x.lower() for x in v]
            elif k in ("auto_download", "include_saved_messages", "include_bots", "download_all_documents"):
                v = bool(v)
            elif k == "library_dir":
                v = str(v).strip()
                if not v:
                    raise ValueError("library_dir is required")
            elif k == "api_hash":
                v = str(v).strip()
            else:
                continue
            if getattr(s, k) != v:
                setattr(s, k, v)
                changed.add(k)
        if changed:
            self.save()
        return s, changed
