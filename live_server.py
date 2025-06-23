import asyncio
import logging
import httpx
import json
from bs4 import BeautifulSoup
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    ConnectEvent, CommentEvent, FollowEvent, ShareEvent, GiftEvent,
    DisconnectEvent, LiveEndEvent, LikeEvent, JoinEvent, SubscribeEvent
)
import uvicorn
from typing import Any, Dict

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

app = FastAPI()

async def send_json_safe(websocket: WebSocket, data: Dict[str, Any]):
    try:
        await websocket.send_json(data)
    except (WebSocketDisconnect, RuntimeError):
        logging.warning("Failed to send JSON data; connection is closed.")

def parse_live_profile_data(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict) and 'follow_info' in data:
        follow_info = data.get('follow_info', {})
        avatar_thumb = data.get('avatar_thumb', {})
        profile = {
            "nickname": data.get('nickname', 'N/A'),
            "username": data.get('display_id', 'N/A'),
            "avatar": avatar_thumb.get('url_list', [None])[0] if avatar_thumb else None,
            "followers": follow_info.get('follower_count', 0),
            "following": follow_info.get('following_count', 0),
            "bio": data.get('bio_description', '').replace('\n', ' '),
            "likes": data.get("like_count", 0)
        }
        return profile
    return {}

async def get_user_profile_from_web(username: str) -> Dict[str, Any] | None:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}
        async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=True) as web_client:
            response = await web_client.get(f"https://www.tiktok.com/@{username}")
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            script_tag = soup.find('script', id='__UNIVERSAL_DATA_FOR_REHYDRATION__')
            if not script_tag: return None
            
            data = json.loads(script_tag.string)
            user_data_path = data.get('__DEFAULT_SCOPE__', {}).get('webapp.user-detail', {})
            if not user_data_path or not user_data_path.get('userInfo'): return None

            user_info = user_data_path['userInfo']
            user = user_info.get('user', {})
            stats = user_info.get('stats', {})
            
            return {
                "nickname": user.get("nickname", username),
                "username": user.get("uniqueId", username),
                "avatar": user.get("avatarLarger"),
                "followers": stats.get("followerCount", 0),
                "following": stats.get("followingCount", 0),
                "bio": user.get("signature", "Bio not available.").replace('\n', ' '),
                "likes": stats.get("heartCount", 0)
            }
    except Exception as e:
        logging.error(f"Exception during web scraping for @{username}: {e}")
        return None

async def handle_tiktok_events(client: TikTokLiveClient, websocket: WebSocket):
    is_connected = asyncio.Event()

    @client.on(ConnectEvent)
    async def on_connect(_: ConnectEvent):
        logging.info(f"Connected to @{client.unique_id}'s live stream.")
        is_connected.set()

        owner = client.room_info.get('owner')
        profile_data = parse_live_profile_data(owner) if owner else {}
        if profile_data:
            await send_json_safe(websocket, {"type": "profile_info", "data": profile_data})
            await send_json_safe(websocket, {"type": "total_likes_update", "count": profile_data.get("likes", 0)})

        if client.room_info:
            await send_json_safe(websocket, {"type": "room_info_update", "data": client.room_info})
        
        if client.gift_info:
             await send_json_safe(websocket, {"type": "gift_info_update", "data": client.gift_info})

        await send_json_safe(websocket, {"type": "status_update", "status": "live"})
        await send_json_safe(websocket, {"type": "system_status", "status": "Connected & Listening", "level": "live"})
    
    event_handlers = {
        CommentEvent: lambda e: {"type": "comment", "user": e.user.unique_id, "comment": e.comment},
        LikeEvent: lambda e: {"type": "like", "user": e.user.unique_id, "count": e.count},
        FollowEvent: lambda e: {"type": "follow", "user": e.user.unique_id},
        ShareEvent: lambda e: {"type": "share", "user": e.user.unique_id},
        JoinEvent: lambda e: {"type": "join", "user": e.user.unique_id},
        SubscribeEvent: lambda e: {"type": "subscribe", "user": e.user.unique_id},
        DisconnectEvent: lambda e: {"type": "system_status", "status": "Stream Disconnected", "level": "disconnected"},
        LiveEndEvent: lambda e: {"type": "system_status", "status": "Livestream Ended", "level": "ended"},
    }

    for event, func in event_handlers.items():
        client.add_listener(event, lambda e, f=func: asyncio.create_task(send_json_safe(websocket, f(e))))

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        if not event.gift.streakable or (event.gift.streakable and not event.streaking):
            await send_json_safe(websocket, {"type": "gift", "user": event.user.unique_id, "gift_name": event.gift.name, "count": event.repeat_count})

    try:
        await client.start(fetch_room_info=True, fetch_gift_info=True)
    except Exception as e:
        logging.error(f"Failed to start TikTok client for @{client.unique_id}: {e}")
    finally:
        if not is_connected.is_set():
            is_connected.set()

async def handle_offline_user(username: str, websocket: WebSocket):
    await send_json_safe(websocket, {"type": "system_status", "status": f"User is offline. Scraping profile...", "level": "info"})
    profile_data = await get_user_profile_from_web(username)
    if not profile_data:
        profile_data = {"nickname": username, "username": username}
        await send_json_safe(websocket, {"type": "system_status", "status": f"Could not retrieve profile for @{username}.", "level": "error"})
    
    await send_json_safe(websocket, {"type": "profile_info", "data": profile_data})
    await send_json_safe(websocket, {"type": "status_update", "status": "offline"})

@app.get("/")
async def read_root():
    return HTMLResponse(content="""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>TikTok Overlay</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-gray-900 text-white flex items-center justify-center h-screen font-sans"><div class="text-center bg-gray-800 p-10 rounded-lg shadow-2xl"><h1 class="text-4xl font-bold mb-4">Dynamic TikTok Live Overlay</h1><p class="text-lg text-gray-300">To use, navigate to the URL for the user you want to track.</p><p class="mt-4 bg-gray-700 p-3 rounded">For example: <strong class="text-white">/username</strong></p></div></body></html>""")

@app.get("/{username}")
async def get_overlay_for_user(username: str):
    try:
        with open("overlay.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: overlay.html not found.</h1>", status_code=500)

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await websocket.accept()
    client = TikTokLiveClient(unique_id=f"@{username.lower()}")
    
    is_live = False
    try:
        is_live = await client.is_live()
    except Exception as e:
        logging.error(f"Error checking live status for @{username}: {e}")

    if is_live:
        tiktok_task = asyncio.create_task(handle_tiktok_events(client, websocket))
    else:
        tiktok_task = asyncio.create_task(handle_offline_user(username, websocket))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logging.warning(f"WebSocket client for @{username} disconnected.")
    finally:
        if client.connected:
            await client.disconnect()
        tiktok_task.cancel()
        logging.info(f"Connection closed and tasks cleaned up for @{username}.")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)