# TelegramDownloader

Watches your **private Telegram chats** (as your own user account). When a message contains a video, or a link to
a message that has one (e.g. `https://t.me/c/3215507776/11173`), it downloads the file and files it into a
library:

```
TelegramLibrary\
  Series\Fauda\Season 01\Fauda - S01E02.mkv
  Series\הבורר\Season 02\הבורר - S02E05.mp4
  Movies\Inception (2010)\Inception (2010).mkv
  _Unsorted\<files it could not identify>
```

A local web UI (http://127.0.0.1:8765) lets you browse and play the library, watch download progress, add links
by hand and change all settings, including the Telegram login.

## What it handles

- Videos sent or forwarded directly into a private chat (GIFs, stickers, round video notes and voice notes are ignored).
- `t.me/c/<id>/<msg>` links (private channels/groups **your account has joined**), `t.me/c/<id>/<topic>/<msg>`, and
  public `t.me/<channel>/<msg>` links, in plain text, hidden text links or inline buttons.
- Saved Messages and chats with bots (both can be switched off), or only a list of chats you choose.
- Episode/movie detection from the file name, falling back to the caption. English (`S01E02`, `1x02`, `Season 1
  Episode 2`) and Hebrew (`עונה 2 פרק 5`, `ע2 פ5`, `פרק 3`) are both understood. Anything it can't identify goes to
  `_Unsorted`, where **Move** in the UI files it correctly.
- Resumable downloads: an interrupted file continues from its `.part` file in `<library>\.incomplete`. Telegram rate
  limits (FloodWait), expired file references and network errors are retried automatically.
- No duplicates: the same message, or the same file forwarded twice, is downloaded once.
- **Scan history** (Downloads tab) goes back through the last N messages of every private chat.

## Install (Windows)

Requires Python 3.10+.

```bat
cd "C:\Source Code\windsurf\Python"
git clone https://github.com/mustirabin/TelegramDownloader.git
cd TelegramDownloader
py -m venv .venv
.venv\Scripts\activate
pip install .
```

## First run

1. Get an `api_id` and `api_hash` (once, free): go to https://my.telegram.org, then **API development tools**, and
   create an app (any name).
2. Run:
   ```bat
   .venv\Scripts\activate
   tgdl
   ```
   The browser opens at http://127.0.0.1:8765.
3. **Settings**: paste `api_id` / `api_hash`, click **Save & connect**, then enter your phone number, the code
   Telegram sends you, and your 2FA password if you have one.
4. Set the library folder (default `%USERPROFILE%\Videos\TelegramLibrary`) and the other options, then **Save settings**.

Keep `tgdl` running; new messages are picked up live. Options: `tgdl --port 9000`, `tgdl --no-browser`,
`tgdl --data-dir D:\tgdl-data`.

## Security notes

- `data\tgdl.session` is a logged-in Telegram session, **equivalent to full access to your account**. Don't share
  it or put it in a synced folder. **Log out** in Settings (or Telegram, then Settings, then Devices) revokes it.
- `data\settings.json` holds `api_hash`. You can instead set the `TG_API_ID` / `TG_API_HASH` environment
  variables. `data\` is git-ignored.
- The web UI only listens on `127.0.0.1` and has no remote access. It rejects requests with a foreign `Host` header
  (DNS rebinding) and requires a custom header on every change, so other websites open in your browser can't
  control it.

## Development

```bat
pip install -e .[dev]
pytest
```
