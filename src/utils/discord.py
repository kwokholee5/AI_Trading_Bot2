import httpx
import asyncio
from datetime import datetime

WEBHOOK_URL = "https://discord.com/api/webhooks/1435545693833007226/Z2dXZ2Cx5PHN3hqjnF9xddZwZgPY25iy9jGV77cic5xULsqat-tbxntNYd7bxjADtiRB"


async def send_discord(content: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "content": f"{content}",
        "username": "DeepSeekBit",
        "avatar_url": "https://www.tw-pool.com/static/icons/ore.png",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(WEBHOOK_URL, json=payload)
        r.raise_for_status()


def notify_discord(content: str):
    """同步程式方便使用"""
    try:
        asyncio.run(send_discord(content))
    except RuntimeError:
        # 若 loop 已存在（如在 async 環境中）
        loop = asyncio.get_event_loop()
        loop.create_task(send_discord(content))