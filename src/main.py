"""Telegram -> Discord bridge. Text first, album-aware, one-time Archive 99 repair."""

import asyncio
import json
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from telethon import TelegramClient
from telethon.sessions import StringSession

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "data" / "state.json"

# One-time repair for the historical post in the user's screenshot.
# Once successfully sent, GitHub Actions persists the marker in data/state.json.
REPLAY_SOURCE = "furryboringnews"
REPLAY_TITLE = "Archive Number: 99"
REPLAY_MARKER = "archive99_caption_replay_20261119_v1"
REPLAY_PATTERN = re.compile(r"Archive\s*Number\s*:\s*99\b", re.I)
DISCORD_TEXT_LIMIT = 1900
DISCORD_FILE_LIMIT = 10


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def get_channel_reference(value):
    value = value.strip()
    if "t.me/" in value:
        parts = urlparse(value if "://" in value else "https://" + value).path.strip("/").split("/")
        if parts and parts[0] == "s":
            parts.pop(0)
        if not parts or parts[0].startswith("+"):
            raise ValueError("Only public Telegram channels are supported")
        value = parts[0]
    if not value.startswith("@") and not value.lstrip("-").isdigit():
        value = "@" + value
    return value


def message_groups(messages):
    groups = {}
    for msg in messages:
        if msg is None:
            continue
        group_id = getattr(msg, "grouped_id", None)
        key = ("album", group_id) if group_id else ("message", msg.id)
        groups.setdefault(key, []).append(msg)
    return [sorted(group, key=lambda m: m.id)
            for group in sorted(groups.values(), key=lambda g: min(m.id for m in g))]


async def complete_album(client, entity, messages):
    """Retrieve the caption-carrying album member even if it predates min_id."""
    if not messages:
        return []
    group_id = getattr(messages[0], "grouped_id", None)
    if not group_id:
        return messages
    seen = {m.id: m for m in messages}
    # Telegram albums have up to 10 members; a 12-ID margin both ways covers
    # the common case where a polling checkpoint cuts through the album.
    anchor = min(seen)
    nearby_ids = list(range(max(1, anchor - 12), anchor + 13))
    try:
        nearby = await client.get_messages(entity, ids=nearby_ids)
        for msg in nearby:
            if msg is not None and getattr(msg, "grouped_id", None) == group_id:
                seen[msg.id] = msg
    except Exception as exc:
        print(f"[WARN] Album reconstruction failed at ID {anchor}: {type(exc).__name__}")
    result = sorted(seen.values(), key=lambda m: m.id)
    print("[ALBUM] " + ", ".join(
        f"{m.id}:text={len(getattr(m, 'message', None) or '')}" for m in result
    ))
    return result


def get_link(entity, message_id):
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{entity.id}/{message_id}"


def extract_body(messages):
    """Return actual Telegram captions/text; no fabricated description."""
    paragraphs = []
    previews = []
    hidden_urls = []
    for msg in messages:
        txt = (getattr(msg, "message", None) or "").strip()
        if txt and txt not in paragraphs:
            paragraphs.append(txt)
        for entity in getattr(msg, "entities", None) or []:
            uri = getattr(entity, "url", None)
            if uri and uri not in hidden_urls:
                hidden_urls.append(uri)
        webpage = getattr(getattr(msg, "media", None), "webpage", None)
        if webpage:
            details = []
            for field in ("site_name", "title", "description", "url"):
                value = getattr(webpage, field, None)
                if value and value not in details:
                    details.append(value)
            if details:
                snippet = "\n".join(details)
                if snippet not in previews:
                    previews.append(snippet)
    body = "\n\n".join(paragraphs)
    if previews and not body:
        body = "网页预览：\n" + "\n\n".join(previews)
    if hidden_urls:
        extras = [url for url in hidden_urls if url not in body]
        if extras:
            body += ("\n\n" if body else "") + "原帖链接：\n" + "\n".join(extras)
    return body.strip()


def friendly_size(size):
    return f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{size / 1024:.1f} KB"


async def get_attachments(client, messages, max_file, max_total):
    uploads, notes = [], []
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        for msg in messages:
            if getattr(msg, "noforwards", False):
                notes.append("部分媒体受到保护，未复制附件。")
                continue
            file = getattr(msg, "file", None)
            if not file:
                continue
            name = getattr(file, "name", None) or f"telegram_{msg.id}{getattr(file, 'ext', '') or ''}"
            size = getattr(file, "size", None) or 0
            if size > max_file:
                notes.append(f"跳过大附件：{name}（{friendly_size(size)}）")
                continue
            if len(uploads) >= DISCORD_FILE_LIMIT:
                notes.append("剩余附件超过 Discord 每条消息的数量限制。")
                break
            try:
                path = await client.download_media(msg, file=tmp)
                if not path:
                    notes.append(f"下载失败：{name}")
                    continue
                data = Path(path).read_bytes()
                if len(data) > max_file or len(data) + total > max_total:
                    notes.append(f"跳过附件：{name}（超过上传大小限制）")
                    continue
                file_name = re.sub(r"[^\w.\-]+", "_", Path(name).name)[:100]
                mime = getattr(file, "mime_type", None) or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
                uploads.append((file_name, data, mime))
                total += len(data)
            except Exception as exc:
                notes.append(f"附件处理失败：{name}")
                print(f"[WARN] Attachment download issue on {msg.id}: {type(exc).__name__}")
    return uploads, notes


def split_body(body, budget):
    pieces = []
    remaining = body
    while len(remaining) > budget:
        position = remaining.rfind("\n", 0, budget + 1)
        if position >= budget // 2:
            position += 1
        else:
            position = budget
        pieces.append(remaining[:position])
        remaining = remaining[position:]
    if remaining or not pieces:
        pieces.append(remaining)
    return pieces


def make_posts(name, body, url, notes, replay=False):
    header = f"**{name} · 历史内容补发**" if replay else f"**{name}**"
    footer = f"[查看 Telegram 原文]({url})"
    notes = list(dict.fromkeys(notes))
    if len(notes) > 3:
        notes = notes[:3] + ["更多附件未上传，请查看原帖。"]
    notes_text = "\n".join("• " + note for note in notes)
    if len(notes_text) > 350:
        notes_text = notes_text[:347] + "..."
    budget = DISCORD_TEXT_LIMIT - len(header) - len(footer) - len(notes_text) - 80
    chunks = split_body(body, max(500, budget))
    posts = []
    for idx, chunk in enumerate(chunks):
        heading = header if idx == 0 else f"**{name} · 正文续 {idx + 1}/{len(chunks)}**"
        elements = [heading, chunk]
        if idx == 0 and notes_text:
            elements.append("附件说明：\n" + notes_text)
        elements.append(footer)
        post = "\n\n".join(elements)
        if len(post) > DISCORD_TEXT_LIMIT:
            raise ValueError("Internal Discord character limit error")
        posts.append(post)
    return posts


class DiscordError(Exception):
    def __init__(self, status):
        super().__init__(f"Discord HTTP {status}")
        self.status = status


async def send_webhook(session, url, username, text, attachments=()):
    url = url + ("&wait=true" if "?" in url else "?wait=true")
    for attempt in range(5):
        form = aiohttp.FormData()
        form.add_field(
            "payload_json",
            json.dumps({
                "username": username[:80],
                "content": text,
                "allowed_mentions": {"parse": []},
            }, ensure_ascii=False),
            content_type="application/json",
        )
        for i, (name, data, mime) in enumerate(attachments):
            form.add_field(f"files[{i}]", data, filename=name, content_type=mime)
        try:
            async with session.post(url, data=form) as response:
                if response.status in (200, 204):
                    return
                if response.status == 429 and attempt < 4:
                    try:
                        retry_after = float((await response.json()).get("retry_after", 3))
                    except Exception:
                        retry_after = 3
                    await asyncio.sleep(min(60, max(1, retry_after + 1)))
                    continue
                if response.status >= 500 and attempt < 4:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise DiscordError(response.status)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt == 4:
                raise
            await asyncio.sleep(2 ** attempt)


async def forward_group(client, session, entity, group, webhook, name, poll, replay=False):
    group = await complete_album(client, entity, group)
    body = extract_body(group)
    if not body:
        print("[WARN] No caption among message IDs: " + ",".join(str(m.id) for m in group))
        if replay:
            raise RuntimeError("The requested historical post has no caption in Telegram API")
        body = "（原帖没有可提取的文字；请查看 Telegram 原文。）"
    url = get_link(entity, min(m.id for m in group))
    max_file = int(float(poll.get("max_file_mb", 8)) * 1024 * 1024)
    max_total = int(float(poll.get("max_album_total_mb", 20)) * 1024 * 1024)
    try:
        attachments, notes = await get_attachments(client, group, max_file, max_total)
    except Exception as exc:
        print(f"[WARN] Media failed, keeping body: {type(exc).__name__}")
        attachments, notes = [], ["媒体处理失败；正文已保留。"]
    posts = make_posts(name, body, url, notes, replay=replay)
    for i, post in enumerate(posts):
        files = attachments if i == 0 else ()
        try:
            await send_webhook(session, webhook, f"Telegram · {name}", post, files)
        except DiscordError as exc:
            if files and exc.status in (400, 413):
                print("[WARN] Discord refused attachments; retrying this part as text")
                await send_webhook(session, webhook, f"Telegram · {name}", post)
            else:
                raise
    print(f"[SENT] {name}: {min(m.id for m in group)}-{max(m.id for m in group)} "
          f"parts={len(posts)} text_length={len(body)} replay={replay}")
    return max(m.id for m in group)


async def find_archive_99(client, entity):
    """Find the EXACT historical post, not just the newest unrelated item."""
    for query in (REPLAY_TITLE, "Archive Number", "99"):
        try:
            async for msg in client.iter_messages(entity, search=query, limit=150):
                if REPLAY_PATTERN.search(getattr(msg, "message", None) or ""):
                    return msg
        except Exception as exc:
            print(f"[WARN] Telegram search unavailable for query {query!r}: {type(exc).__name__}")
    # Telegram server-side search is sometimes unavailable; inspect recent posts.
    async for msg in client.iter_messages(entity, limit=250):
        if REPLAY_PATTERN.search(getattr(msg, "message", None) or ""):
            return msg
    return None


async def sync_channel(client, session, source, poll, state):
    name = source.get("name", source["telegram"])
    secret = source["webhook_secret"]
    webhook = os.environ.get(secret, "").strip()
    if not webhook or not webhook.startswith("https://discord.com/api/webhooks/"):
        raise RuntimeError(f"Missing or invalid Discord webhook Secret: {secret}")
    channel_ref = get_channel_reference(source["telegram"])
    entity = await client.get_entity(channel_ref)
    channel_id = str(entity.id)
    last_id = state["last_message_id"].get(channel_id)

    # One-time targeted correction. After this run, resume ordinary polling.
    replay_flags = state.setdefault("completed_replays", {})
    if channel_ref.lower().lstrip("@") == REPLAY_SOURCE and not replay_flags.get(REPLAY_MARKER):
        print(f"[REPLAY] Looking for {REPLAY_TITLE} in @{REPLAY_SOURCE}")
        target = await find_archive_99(client, entity)
        if target is None:
            raise RuntimeError(f"Could not find '{REPLAY_TITLE}'; no unrelated posts were sent")
        latest = await client.get_messages(entity, limit=1)
        latest_id = latest[0].id if latest else target.id
        await forward_group(client, session, entity, [target], webhook, name, poll, replay=True)
        # Start future sync from the current channel tip to avoid a historical flood.
        replay_flags[REPLAY_MARKER] = True
        state["last_message_id"][channel_id] = max(int(last_id or 0), latest_id)
        save_state(state)
        print(f"[REPLAY DONE] {REPLAY_TITLE}; future cursor={state['last_message_id'][channel_id]}")
        return

    if last_id is None:
        latest = await client.get_messages(entity, limit=1)
        state["last_message_id"][channel_id] = latest[0].id if latest else 0
        save_state(state)
        print(f"[BASELINE] {name}: initialized")
        return

    limit = max(1, int(poll.get("max_messages_per_channel", 100)))
    messages = [m async for m in client.iter_messages(
        entity, min_id=int(last_id), limit=limit, reverse=True
    )]
    if not messages:
        print(f"[OK] {name}: no new messages")
        return

    for group in message_groups(messages):
        high_id = await forward_group(client, session, entity, group, webhook, name, poll)
        state["last_message_id"][channel_id] = max(high_id, int(state["last_message_id"].get(channel_id, 0)))
        save_state(state)


async def main():
    config = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {"last_message_id": {}})
    state.setdefault("last_message_id", {})
    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]
    if not sources:
        print("[INFO] No Telegram sources configured")
        return
    client = TelegramClient(
        StringSession(os.environ["TELEGRAM_SESSION"]),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
        connection_retries=5, request_retries=3, auto_reconnect=True,
        flood_sleep_threshold=60,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session not authorized")
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            failures = []
            for source in sources:
                try:
                    await sync_channel(client, session, source, config.get("poll", {}), state)
                except Exception as exc:
                    print(f"[ERROR] {source.get('name', 'unknown')}: {exc}")
                    failures.append(source.get("name", "unknown"))
            save_state(state)
            if failures:
                raise RuntimeError("Sync failed for: " + ", ".join(failures))
    finally:
        save_state(state)
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
