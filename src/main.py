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
