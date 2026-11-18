
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
LIMIT = 1900


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temp.replace(STATE_FILE)


def get_channel_reference(value):
    value = value.strip()
    if "t.me/" in value:
        parts = urlparse(
            value if "://" in value else "https://" + value
        ).path.strip("/").split("/")
        if parts and parts[0] == "s":
            parts.pop(0)
        if not parts or parts[0].startswith("+"):
            raise ValueError("Only public channels are supported")
        value = parts[0]

    if not value.startswith("@") and not value.lstrip("-").isdigit():
        value = "@" + value
    return value


def group_messages(messages):
    groups = {}
    for message in messages:
        group_id = getattr(message, "grouped_id", None)
        key = f"album_{group_id}" if group_id else f"single_{message.id}"
        groups.setdefault(key, []).append(message)

    result = list(groups.values())
    for group in result:
        group.sort(key=lambda m: m.id)
    return sorted(result, key=lambda g: g[0].id)


def telegram_link(entity, message_id):
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{entity.id}/{message_id}"


def extract_text(messages):
    texts = []
    previews = []
    hidden_links = []
    seen = set()

    for message in messages:
        body = (getattr(message, "message", None) or "").strip()

        if body and body not in seen:
            texts.append(body)
            seen.add(body)

        for entity in getattr(message, "entities", None) or []:
            url = getattr(entity, "url", None)
            if url and url not in hidden_links:
                hidden_links.append(url)

        media = getattr(message, "media", None)
        webpage = getattr(media, "webpage", None)

        if webpage:
            site = getattr(webpage, "site_name", None)
            title = getattr(webpage, "title", None)
            description = getattr(webpage, "description", None)
            url = getattr(webpage, "url", None)

            lines = []
            if site:
                lines.append(site)
            if title:
                lines.append(f"**{title}**")
            if description:
                lines.append(description)
            if url:
                lines.append(url)

            preview = "\n".join(lines).strip()
            if preview and preview not in previews:
                previews.append(preview)

    result = "\n\n".join(texts)

    if previews:
        result += (
            "\n\n**网页预览**\n\n"
            + "\n\n".join(previews)
        )

    visible = "\n".join(texts)
    hidden = [u for u in hidden_links if u not in visible]

    if hidden:
        result += "\n\n**原帖链接**\n" + "\n".join(hidden)

    return result.strip()


def human_size(size):
    size = float(size or 0)
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024:.1f} KB"


async def download_media(client, messages, max_file, max_total):
    attachments = []
    notes = []
    total = 0

    with tempfile.TemporaryDirectory() as temp:
        for message in messages:
            if not getattr(message, "media", None):
                continue

            if getattr(message, "noforwards", False):
                notes.append("该消息开启内容保护，未复制附件。")
                continue

            info = getattr(message, "file", None)
            if not info:
                continue

            name = getattr(info, "name", None) or f"message-{message.id}"
            size = getattr(info, "size", None) or 0

            if size > max_file:
                notes.append(
                    f"附件未上传：{name}（{human_size(size)}）"
                )
                continue

            if len(attachments) >= 10:
                notes.append("其余附件未上传：已达到单条消息附件数量限制。")
                continue

            try:
                path = await client.download_media(message, file=temp)

                if not path:
                    notes.append(f"附件下载失败：{name}")
                    continue

                file_path = Path(path)
                data = file_path.read_bytes()

                if len(data) > max_file:
                    notes.append(
                        f"附件未上传：{name}（{human_size(len(data))}）"
                    )
                    continue

                if total + len(data) > max_total:
                    notes.append(
                        f"附件未上传：{name}（超过媒体总大小限制）"
                    )
                    continue

                filename = re.sub(
                    r"[^\w.\-]+",
                    "_",
                    Path(name or file_path.name).name
                )[:100]

                mime = (
                    getattr(info, "mime_type", None)
                    or mimetypes.guess_type(filename)[0]
                    or "application/octet-stream"
                )

                attachments.append((filename, data, mime))
                total += len(data)

            except Exception as exc:
                print(f"[WARN] Media download failed: {exc}")
                notes.append(f"附件下载失败：{name}")

    return attachments, notes


def split_text(text, limit):
    chunks = []
    remaining = text or "（原帖没有可提取的文字内容。）"

    while len(remaining) > limit:
        position = remaining.rfind("\n", 0, limit)
        if position < limit // 2:
            position = remaining.rfind(" ", 0, limit)
        if position < limit // 2:
            position = limit

        chunks.append(remaining[:position])
        remaining = remaining[position:]

    if remaining:
        chunks.append(remaining)

    return chunks


def build_messages(name, body, link, notes):
    header = f"**{name}**"
    footer = f"[查看 Telegram 原文]({link})"

    note_text = ""
    if notes:
        note_text = "**附件说明**\n" + "\n".join(
            f"• {note}" for note in notes
        )

    body_limit = LIMIT - len(header) - len(footer) - len(note_text) - 30
    body_limit = max(200, body_limit)

    parts = split_text(body, body_limit)
    result = []

    for index, part in enumerate(parts):
        title = header if index == 0 else f"**{name}（续）**"
        content = f"{title}\n\n{part}"

        if index == 0 and note_text:
            content += f"\n\n{note_text}"

        content += f"\n\n{footer}"

        if len(content) > LIMIT:
            raise RuntimeError("Discord message exceeds character limit")

        result.append(content)

    return result


async def send_discord(session, webhook, username, content, files=()):
    payload = {
        "username": username[:80],
        "content": content,
        "allowed_mentions": {"parse": []}
    }

    url = webhook + (
        "&wait=true" if "?" in webhook else "?wait=true"
    )

    for attempt in range(5):
        form = aiohttp.FormData()
        form.add_field(
            "payload_json",
            json.dumps(payload, ensure_ascii=False),
            content_type="application/json"
        )

        for index, (filename, data, mime) in enumerate(files):
            form.add_field(
                f"files[{index}]",
                data,
                filename=filename,
                content_type=mime
            )

        try:
            async with session.post(url, data=form) as response:
                if response.status in (200, 204):
                    return

                body = await response.text()

                if response.status == 429:
                    try:
                        delay = float(
                            json.loads(body).get("retry_after", 3)
                        )
                    except Exception:
                        delay = 3

                    if attempt == 4:
                        raise RuntimeError(f"Discord HTTP 429: {body[:300]}")

                    await asyncio.sleep(min(delay + 1, 60))
                    continue

                if response.status >= 500 and attempt < 4:
                    await asyncio.sleep(2 ** attempt)
                    continue

                raise RuntimeError(
                    f"Discord HTTP {response.status}: {body[:300]}"
                )

        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt == 4:
                raise
            await asyncio.sleep(2 ** attempt)

    raise RuntimeError("Discord sending failed")


async def sync_channel(client, session, source, poll, state):
    name = source.get("name", source["telegram"])
    secret_name = source["webhook_secret"]
    webhook = os.environ.get(secret_name, "").strip()

    if not webhook:
        raise RuntimeError(f"Missing GitHub Secret: {secret_name}")

    if not webhook.startswith("https://discord.com/api/webhooks/"):
        raise RuntimeError("Invalid Discord webhook")

    entity = await client.get_entity(
        get_channel_reference(source["telegram"])
    )

    channel_id = str(entity.id)
    last_id = state["last_message_id"].get(channel_id)

    # 首次运行只建立基线，不推送历史消息
    if last_id is None:
        latest = await client.get_messages(entity, limit=1)
        state["last_message_id"][channel_id] = (
            latest[0].id if latest else 0
        )
        save_state(state)
        print(f"[BASELINE] {name}: initialized")
        return

    limit = max(1, int(poll.get("max_messages_per_channel", 100)))

    messages = [
        message async for message in client.iter_messages(
            entity,
            min_id=int(last_id),
            limit=limit,
            reverse=True
        )
    ]

    if not messages:
        print(f"[OK] {name}: no new messages")
        return

    max_file = int(float(poll.get("max_file_mb", 8)) * 1024 * 1024)
    max_total = int(
        float(poll.get("max_album_total_mb", 20)) * 1024 * 1024
    )

    for group in group_messages(messages):
        body = extract_text(group)
        has_media = any(getattr(m, "media", None) for m in group)

        if not body:
            body = (
                "（原帖没有文字正文，包含媒体内容。）"
                if has_media else "（原帖没有可提取的文字内容。）"
            )

        link = telegram_link(entity, group[0].id)

        # 媒体失败绝不阻断正文同步
        try:
            files, notes = await download_media(
                client, group, max_file, max_total
            )
        except Exception as exc:
            print(f"[WARN] Media processing failed: {exc}")
            files = []
            notes = ["媒体处理失败，文字正文仍正常同步。"]

        chunks = build_messages(name, body, link, notes)

        try:
            for index, content in enumerate(chunks):
                await send_discord(
                    session,
                    webhook,
                    f"Telegram · {name}",
                    content,
                    files if index == 0 else ()
                )

        except Exception as exc:
            # 如果 Discord 拒绝附件，重新发送纯文字版本
            if files:
                print(f"[WARN] Attachment rejected: {exc}")
                fallback_notes = notes + [
                    "Discord 未接受媒体附件，已保留文字正文。"
                ]
                fallback_chunks = build_messages(
                    name, body, link, fallback_notes
                )

                for content in fallback_chunks:
                    await send_discord(
                        session,
                        webhook,
                        f"Telegram · {name}",
                        content
                    )
            else:
                raise

        state["last_message_id"][channel_id] = max(
            m.id for m in group
        )
        save_state(state)

        print(
            f"[SENT] {name}: {group[0].id}-"
            f"{max(m.id for m in group)}"
        )


async def main():
    config = load_json(CONFIG_FILE, {})
    state = load_json(
        STATE_FILE,
        {"last_message_id": {}}
    )
    state.setdefault("last_message_id", {})

    sources = [
        s for s in config.get("sources", [])
        if s.get("enabled", True)
    ]

    if not sources:
        print("[INFO] No enabled sources")
        return

    client = TelegramClient(
        StringSession(os.environ["TELEGRAM_SESSION"]),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
        connection_retries=5,
        request_retries=3,
        auto_reconnect=True,
        flood_sleep_threshold=60
    )

    await client.connect()

    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session unauthorized")

        timeout = aiohttp.ClientTimeout(total=180)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            errors = []

            for source in sources:
                try:
                    await sync_channel(
                        client,
                        session,
                        source,
                        config.get("poll", {}),
                        state
                    )
                except Exception as exc:
                    print(
                        f"[ERROR] {source.get('name', 'unknown')}: {exc}"
                    )
                    errors.append(source.get("name", "unknown"))

            save_state(state)

            if errors:
                raise RuntimeError(
                    "Failed sources: " + ", ".join(errors)
                )

    finally:
        save_state(state)
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
