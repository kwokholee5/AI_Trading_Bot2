import httpx
import asyncio
import os
from datetime import datetime
from typing import Optional


def get_webhook_url() -> Optional[str]:
    """获取Discord webhook URL from environment variable"""
    return os.getenv('DISCORD_WEBHOOK_URL')


def get_account_tag() -> Optional[str]:
    """获取Discord账户标签 from environment variable"""
    return os.getenv('DISCORD_ACCOUNT_TAG')


async def send_discord(content: str, account_tag: Optional[str] = None):
    """
    发送Discord通知
    
    Args:
        content: 消息内容
        account_tag: 账户标签（可选），用于标识不同的交易账户
    """
    webhook_url = get_webhook_url()
    if not webhook_url:
        print("⚠️ DISCORD_WEBHOOK_URL 未设置，跳过Discord通知")
        return
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 如果有账户标签，添加到消息内容前
    if account_tag:
        content = f"[{account_tag}] {content}"
    
    payload = {
        "content": f"{content}",
        "username": "DeepSeekBit",
        "avatar_url": "https://www.tw-pool.com/static/icons/ore.png",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(webhook_url, json=payload)
        r.raise_for_status()


def notify_discord(content: str, account_tag: Optional[str] = None):
    """
    同步程式方便使用
    
    Args:
        content: 消息内容
        account_tag: 账户标签（可选），如果未提供则从环境变量读取
    """
    # 如果未提供account_tag，尝试从环境变量读取
    if account_tag is None:
        account_tag = get_account_tag()
    
    try:
        asyncio.run(send_discord(content, account_tag))
    except RuntimeError:
        # 若 loop 已存在（如在 async 環境中）
        loop = asyncio.get_event_loop()
        loop.create_task(send_discord(content, account_tag))