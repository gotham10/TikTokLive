import asyncio
import logging
import httpx
import json
from bs4 import BeautifulSoup
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState
from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    ConnectEvent, CommentEvent, FollowEvent, ShareEvent, GiftEvent,
    DisconnectEvent, LiveEndEvent, LikeEvent, JoinEvent, SubscribeEvent
)
from TikTokLive.objects import User
import uvicorn
from typing import Any, Dict
from html import escape

app = FastAPI()

async def send_json_safe(websocket: WebSocket, data: Dict[str, Any]):
    try:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_json(data)
    except (WebSocketDisconnect, RuntimeError):
        logging.warning("Failed to send JSON data; connection is closed.")

def parse_user_data(user: User) -> Dict[str, Any]:
    avatar_url = None
    if user.avatar and user.avatar.url_list:
        avatar_url = user.avatar.url_list[0]

    follow_info = getattr(user, 'follow_info', None)
    followers = follow_info.follower_count if follow_info else 0
    following = follow_info.following_count if follow_info else 0

    return {
        "user": user.unique_id,
        "nickname": user.nickname,
        "avatar": avatar_url,
        "followers": followers,
        "following": following
    }

def parse_live_profile_data(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict) and 'follow_info' in data:
        follow_info = data.get('follow_info', {})
        avatar_thumb = data.get('avatar_thumb', {})
        return {
            "nickname": data.get('nickname', 'N/A'),
            "username": data.get('display_id', 'N/A'),
            "avatar": avatar_thumb.get('url_list', [None])[0] if avatar_thumb else None,
            "followers": follow_info.get('follower_count', 0),
            "following": follow_info.get('following_count', 0),
            "bio": data.get('bio_description', '').replace('\n', ' ')
        }
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
    @client.on(ConnectEvent)
    async def on_connect(_: ConnectEvent):
        logging.info(f"Connected to @{client.unique_id}'s live stream.")
        owner = client.room_info.get('owner')
        profile_data = parse_live_profile_data(owner) if owner else {}
        if profile_data:
            await send_json_safe(websocket, {"type": "profile_info", "data": profile_data})
        initial_likes = client.room_info.get('like_count', 0)
        await send_json_safe(websocket, {"type": "total_likes_update", "count": initial_likes})
        if client.room_info:
            await send_json_safe(websocket, {"type": "room_info_update", "data": client.room_info})
        await send_json_safe(websocket, {"type": "status_update", "status": "live"})
        await send_json_safe(websocket, {"type": "system_status", "status": "Connected & Listening", "level": "live"})

    async def forward_event(data: dict):
        await send_json_safe(websocket, data)

    @client.on(CommentEvent)
    async def on_comment(event: CommentEvent):
        user_data = parse_user_data(event.user)
        user_data.update({"type": "comment", "comment": event.comment})
        await forward_event(user_data)

    @client.on(FollowEvent)
    async def on_follow(event: FollowEvent):
        user_data = parse_user_data(event.user)
        user_data.update({"type": "follow"})
        await forward_event(user_data)

    @client.on(ShareEvent)
    async def on_share(event: ShareEvent):
        user_data = parse_user_data(event.user)
        user_data.update({"type": "share"})
        await forward_event(user_data)
        
    @client.on(JoinEvent)
    async def on_join(event: JoinEvent):
        user_data = parse_user_data(event.user)
        user_data.update({"type": "join"})
        await forward_event(user_data)

    @client.on(SubscribeEvent)
    async def on_subscribe(event: SubscribeEvent):
        user_data = parse_user_data(event.user)
        user_data.update({"type": "subscribe"})
        await forward_event(user_data)

    @client.on(LikeEvent)
    async def on_like(event: LikeEvent):
        user_data = parse_user_data(event.user)
        user_data.update({"type": "like", "count": event.count})
        await forward_event(user_data)
        if hasattr(event, 'total_likes'):
            await forward_event({"type": "total_likes_update", "count": event.total_likes})

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        if event.gift.streakable and event.streaking:
            return
        user_data = parse_user_data(event.user)
        user_data.update({
            "type": "gift",
            "gift_name": event.gift.name,
            "count": event.repeat_count,
            "coins": event.gift.diamond_count,
            "gift_image_url": event.gift.image.url_list[0] if event.gift.image else None,
            "userId": event.user.id
        })
        await forward_event(user_data)
    
    @client.on(LiveEndEvent)
    async def on_live_end(event: LiveEndEvent):
        await forward_event({"type": "status_update", "status": "ended"})

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        logging.warning(f"Disconnected from @{client.unique_id}'s stream.")
        await forward_event({"type": "system_status", "status": "Stream Disconnected", "level": "disconnected"})

    await client.start(fetch_room_info=True)

async def handle_offline_user(username: str, websocket: WebSocket):
    await send_json_safe(websocket, {"type": "system_status", "status": f"User is offline. Scraping profile...", "level": "info"})
    profile_data = await get_user_profile_from_web(username)
    if not profile_data:
        profile_data = {"nickname": username, "username": username, "likes": 0}
        await send_json_safe(websocket, {"type": "system_status", "status": f"Could not retrieve profile for @{username}.", "level": "error"})
    
    total_likes = profile_data.pop("likes", 0)
    await send_json_safe(websocket, {"type": "profile_info", "data": profile_data})
    await send_json_safe(websocket, {"type": "total_likes_update", "count": total_likes})
    await send_json_safe(websocket, {"type": "status_update", "status": "offline"})

@app.get("/")
async def read_root():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/{username}")
async def get_overlay_for_user(username: str):
    try:
        with open("overlay.html", "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: overlay.html not found.</h1>", status_code=500)

    profile_data = await get_user_profile_from_web(username)
    if profile_data:
        title = escape(f"{profile_data.get('nickname', username)}'s Live | TikTok API Tracker")
        description = escape(profile_data.get('bio', "Track any TikTok user's livestream instantly."))
        icon = escape(profile_data.get('avatar', ''))
        html_content = html_content.replace("__PAGE_TITLE__", title).replace("__PAGE_DESCRIPTION__", description).replace("__PAGE_ICON__", icon)
    else:
        html_content = html_content.replace("__PAGE_TITLE__", f"@{username} | TikTok API Tracker")
    
    return HTMLResponse(content=html_content)

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await websocket.accept()
    client = TikTokLiveClient(unique_id=f"@{username.lower()}")
    tiktok_task = None
    try:
        is_live = await client.is_live()
        if is_live:
            tiktok_task = asyncio.create_task(handle_tiktok_events(client, websocket))
        else:
            tiktok_task = asyncio.create_task(handle_offline_user(username, websocket))
        
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logging.warning(f"WebSocket client for @{username} disconnected.")
    except Exception as e:
        logging.error(f"An unexpected error occurred for @{username}: {e}")
    finally:
        if tiktok_task and not tiktok_task.done():
            tiktok_task.cancel()
        if client.connected:
            await client.disconnect()
        logging.info(f"Connection closed and tasks cleaned up for @{username}.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8000)
