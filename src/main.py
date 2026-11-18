
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
            raise ValueError("Only public Telegram channels are supported")

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


async def download_media(client, messages, max_file, max_total):
    attachments = []
    notes = []

    with tempfile.TemporaryDirectory() as temp_dir:
        for message in messages:
            if not message.media:
                continue

            if getattr(message, "noforwards", False):
                notes.append("内容受保护，未复制媒体附件。")
                continue

            info = getattr(message, "file", None)
            if not info:
                continue

            size = getattr(info, "size", None) or 0

            if size > max_file:
                notes.append("有附件超过单文件大小限制，未上传。")
                continue

            try:
                path = await client.download_media(
                    message,
                    file=temp_dir
                )

                if not path:
                    notes.append(f"消息 {message.id} 的附件下载失败。")
                    continue

                file_path = Path(path)
                data = file_path.read_bytes()

                if len(data) > max_file:
                    notes.append("附件超过大小限制，未上传。")
                    continue

                current_total = sum(len(item[1]) for item in attachments)

                if current_total + len(data) > max_total:
                    notes.append("媒体总大小超过限制，部分附件未上传。")
                    break

                filename = (
                    getattr(info, "name", None)
                    or file_path.name
                )

                filename = re.sub(
                    r"[^\w.\-]+",
                    "_",
                    Path(filename).name
                )[:100]

                mime = (
                    getattr(info, "mime_type", None)
                    or mimetypes.guess_type(filename)[0]
                    or "application/octet-stream"
                )

                attachments.append((filename, data, mime))

            except Exception as exc:
                print(f"[WARN] Media download failed: {exc}")
                notes.append(f"消息 {message.id} 的附件下载失败。")

    return attachments, notes


async def send_discord(
    session,
    webhook,
    username,
    content,
    attachments
):
    payload = {
        "username": username[:80],
        "content": content[:1900],
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

        for index, (filename, data, mime) in enumerate(attachments):
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
                        retry = float(
                            json.loads(body).get("retry_after", 3)
                        )
                    except Exception:
                        retry = 3

                    await asyncio.sleep(min(retry + 1, 60))
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


async def sync_channel(
    client,
    session,
    source,
    poll,
    state
):
    name = source.get("name", source["telegram"])
    webhook_name = source["webhook_secret"]
    webhook = os.environ.get(webhook_name, "").strip()

    if not webhook:
        raise RuntimeError(
            f"Missing GitHub Secret: {webhook_name}"
        )

    if not webhook.startswith("https://discord.com/api/webhooks/"):
        raise RuntimeError(f"Invalid Discord webhook: {webhook_name}")

    entity = await client.get_entity(
        get_channel_reference(source["telegram"])
    )

    channel_id = str(entity.id)
    last_id = state["last_message_id"].get(channel_id)

    # 首次运行只记录最新消息，不推送历史消息
    if last_id is None:
        latest = await client.get_messages(entity, limit=1)

        state["last_message_id"][channel_id] = (
            latest[0].id if latest else 0
        )

        save_state(state)
        print(f"[BASELINE] {name}: initialized")
        return

    limit = int(poll.get("max_messages_per_channel", 100))

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

    max_file = int(
        float(poll.get("max_file_mb", 8)) * 1024 * 1024
    )

    max_total = int(
        float(poll.get("max_album_total_mb", 20)) * 1024 * 1024
    )

    for group in group_messages(messages):
        caption = next(
            (
                m.message for m in group
                if getattr(m, "message", None)
            ),
            ""
        )

        has_media = any(m.media for m in group)

        attachments, notes = await download_media(
            client,
            group,
            max_file,
            max_total
        )

        link = telegram_link(entity, group[0].id)

        body = caption.strip() or (
            "（媒体消息）" if has_media else "（无文本内容）"
        )

        content_parts = [
            f"**{name}**",
            body
        ]

        if notes:
            content_parts.extend(notes)

        content_parts.append(f"[查看 Telegram 原文]({link})")

        content = "\n\n".join(content_parts)

        if len(content) > 1900:
            content = content[:1750] + f"\n\n[查看原文]({link})"

        await send_discord(
            session,
            webhook,
            f"Telegram · {name}",
            content,
            attachments
        )

        state["last_message_id"][channel_id] = max(
            m.id for m in group
        )

        save_state(state)

        print(
            f"[SENT] {name}: message {group[0].id}"
        )


async def main():
    config = load_json(CONFIG_FILE, {})
    state = load_json(
        STATE_FILE,
        {"last_message_id": {}}
    )

    sources = [
        s for s in config.get("sources", [])
        if s.get("enabled", True)
    ]

    if not sources:
        print("[INFO] No Telegram sources configured.")
        return

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_string = os.environ["TELEGRAM_SESSION"]

    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        connection_retries=5,
        request_retries=3,
        auto_reconnect=True,
        flood_sleep_threshold=60
    )

    await client.connect()

    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is unauthorized.")

        timeout = aiohttp.ClientTimeout(total=180)

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

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
                    errors.append(str(exc))

            save_state(state)

            if errors:
                raise RuntimeError(
                    f"{len(errors)} source(s) failed."
                )

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
