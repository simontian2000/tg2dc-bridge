
import asyncio
from getpass import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=== Telegram Session Generator ===")

    api_id = int(input("请输入 API_ID：").strip())
    api_hash = getpass("请输入 API_HASH：").strip()

    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash
    )

    try:
        await client.start(
            phone=lambda: input("请输入手机号（含国家代码）：").strip(),
            code_callback=lambda: input("请输入 Telegram 验证码：").strip(),
            password=lambda: getpass("请输入两步验证密码：")
        )

        me = await client.get_me()

        print("\n登录成功！")
        print(f"Telegram 用户 ID：{me.id}")

        session_string = client.session.save()

        print("\n========== SESSION ==========")
        print(session_string)
        print("=============================\n")

        print("请将上面的字符串保存到 GitHub Secret：")
        print("TELEGRAM_SESSION")
        print("不要将它发布到公开仓库！")

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
