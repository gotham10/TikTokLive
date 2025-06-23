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
import uvicorn
from typing import Any, Dict
from html import escape

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

app = FastAPI()

async def send_json_safe(websocket: WebSocket, data: Dict[str, Any]):
    try:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_json(data)
    except (WebSocketDisconnect, RuntimeError):
        logging.warning(f"Failed to send JSON data for user; connection is closed.")

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
            "bio": data.get('bio_description', '').replace('\n', ' '),
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
        if client.room_info:
            await send_json_safe(websocket, {"type": "room_info_update", "data": client.room_info})
        if client.gift_info:
              await send_json_safe(websocket, {"type": "gift_info_update", "data": client.gift_info})
        await send_json_safe(websocket, {"type": "status_update", "status": "live"})
        await send_json_safe(websocket, {"type": "system_status", "status": "Connected & Listening", "level": "live"})

    async def forward_event(data: dict):
        await send_json_safe(websocket, data)

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
        client.add_listener(event, lambda e, f=func: asyncio.create_task(forward_event(f(e))))

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        if not event.gift.streakable or (event.gift.streakable and not event.streaking):
            await forward_event({"type": "gift", "user": event.user.unique_id, "gift_name": event.gift.name, "count": event.repeat_count})

    await client.start(fetch_room_info=True, fetch_gift_info=True)

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
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>TikTok Live Tracker</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css">
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
        <link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAFQAAABUCAMAAAArteDzAAAA/FBMVEUAAAAl8+3/KFj+LFUn8+//MFgl9e4l9O7+LFX+LVX/MFAl9O4l9O79LFYg+e8l9O7+LVX/LFQo9+/9Llf/LVP9K1X9LFQn9e7/K1YHBwf+LFb9LVT/LVYn9O8m9O7+LVYl9O7+LFUm9e79LFUk8u7/MFUAAAAg0cy4MU5tEyUlBw1uXGrWJkkm8+7/LFYcubMg0c0Yop//LVYAAAAl9O7+LFUFHx5gESAg1dB/Fiu+IUCeGzUj5d/uKVBvEyUSenfeJ0uPGTAQAwUJPTwHLi1ACxUCDw8gBgsXmJWuHjsUiYYOXFoexsEbsq3OJEVPDhowCBEQa2gLTEsMTUvQL1NGAAAAM3RSTlMAYCC/QCCfgO9gEL+PgBDv30AgcFCfgG8w6q+gjzDf38/Pr5BQMCDf39/fz8+vgH9wYFAMSP/SAAAC50lEQVRYw+3YZ3eiQBSA4YkUgQjWGNPrZnuFGyEI1liSTd3//192YDF3jG5AmJz9sL5fPfOcCygMkn+aIWJ7vNA1C5NfA13nidpRb3O5HDf03IzyAHLc0clroE2AH9zRngc6d9R0QOCP9jqgcUfNS9C5oZivaNxQzNnliKJa5odiH874odj51wo/FFVbFnmh2HBMb4THcoULij3a9AORE4r1xzZHFHu3vatyRy8AhP8HvWy+AtoEd5QONU43C7IsF8RFKCCbHDXy61ZUfjH65WBytxQqBmIMmpN2wJs07xOiFSRbN1ftjxu7+iKUEMrSbi9ctxOHrhUj8eYhRBwAWIxS9uQAwmLQQkSO+yYtBqU1dEGJQ+VoSro+DsW0ck2txZmtBxOLQePLh6Z9bnJCcWP7E00OaGUf5+SGymhyQytWUB+5e991Xd9Ni+KgV7gFu4CoDKgRHvxwanYBsqF46a+n5i2EKRtVVa1up0Y/B4POzqnUNRKVEg3uI+3pJQpNQSIkG7pnMavCa0QPOisqBr+laNEIaCUtC4qLxtGiO6DVSBYU7yW/mB+RQDihfQZVeaCbzCIfABrZUFyEL0kA848ZRJ2k6CmD9rx59JBBu0lRg0FNF0Bb8DUe4tc4GUr2mV3dyIPy/C2sxfyHADEanrQBviSCPncZrTFzypVkqIj3KJpTmn949fGUJt45FukdGvPV2TnxR2x2KHqS+Plsm0xb342Z7VqL/ZZCOSFqFK3B7OvceiFfOC5Ge7UBO6iyxFaibc6q1lN2YOKgGyRxh62hOdN1xLbaTx+MOkCTkqOVYjQqNnhst6/Zs+LioAlbo6O+nAM4aNLyVy+bzdCsk+XK9+PNElm2b1txx16SyNKd/U3tTUJTeUNSJL1faPoemmnUT83R8yn9DuCxp6sO7l0PxcuuB3/a0Uj6pKPgravrOE530oFppTLJVk2AZwkqyZ5UZdyjepnwqlGr6npVbWhk1apVCfoNtEO7SNpuAb8AAAAASUVORK5CYII=">
        <meta name="title" content="TikTok Live Tracker">
        <meta name="description" content="Track any TikTok user's livestream instantly. Just enter their @username and go.">
        <meta property="og:type" content="website">
        <meta property="og:url" content="https://yourdomain.com/">
        <meta property="og:title" content="TikTok Live Tracker">
        <meta property="og:description" content="Track any TikTok user's livestream instantly. Just enter their @username and go.">
        <meta property="og:image" content="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAFQAAABUCAMAAAArteDzAAAA/FBMVEUAAAAl8+3/KFj+LFUn8+//MFgl9e4l9O7+LFX+LVX/MFAl9O4l9O79LFYg+e8l9O7+LVX/LFQo9+/9Llf/LVP9K1X9LFQn9e7/K1YHBwf+LFb9LVT/LVYn9O8m9O7+LVYl9O7+LFUm9e79LFUk8u7/MFUAAAAg0cy4MU5tEyUlBw1uXGrWJkkm8+7/LFYcubMg0c0Yop//LVYAAAAl9O7+LFUFHx5gESAg1dB/Fiu+IUCeGzUj5d/uKVBvEyUSenfeJ0uPGTAQAwUJPTwHLi1ACxUCDw8gBgsXmJWuHjsUiYYOXFoexsEbsq3OJEVPDhowCBEQa2gLTEsMTUvQL1NGAAAAM3RSTlMAYCC/QCCfgO9gEL+PgBDv30AgcFCfgG8w6q+gjzDf38/Pr5BQMCDf39/fz8+vgH9wYFAMSP/SAAAC50lEQVRYw+3YZ3eiQBSA4YkUgQjWGNPrZnuFGyEI1liSTd3//192YDF3jG5AmJz9sL5fPfOcCygMkn+aIWJ7vNA1C5NfA13nidpRb3O5HDf03IzyAHLc0clroE2AH9zRngc6d9R0QOCP9jqgcUfNS9C5oZivaNxQzNnliKJa5odiH874odj51wo/FFVbFnmh2HBMb4THcoULij3a9AORE4r1xzZHFHu3vatyRy8AhP8HvWy+AtoEd5QONU43C7IsF8RFKCCbHDXy61ZUfjH65WBytxQqBmIMmpN2wJs07xOiFSRbN1ftjxu7+iKUEMrSbi9ctxOHrhUj8eYhRBwAWIxS9uQAwmLQQkSO+yYtBqU1dEGJQ+VoSro+DsW0ck2txZmtBxOLQePLh6Z9bnJCcWP7E00OaGUf5+SGymhyQytWUB+5e991Xd9Ni+KgV7gFu4CoDKgRHvxwanYBsqF46a+n5i2EKRtVVa1up0Y/B4POzqnUNRKVEg3uI+3pJQpNQSIkG7pnMavCa0QPOisqBr+laNEIaCUtC4qLxtGiO6DVSBYU7yW/mB+RQDihfQZVeaCbzCIfABrZUFyEL0kA848ZRJ2k6CmD9rx59JBBu0lRg0FNF0Bb8DUe4tc4GUr2mV3dyIPy/C2sxfyHADEanrQBviSCPncZrTFzypVkqIj3KJpTmn949fGUJt45FukdGvPV2TnxR2x2KHqS+Plsm0xb342Z7VqL/ZZCOSFqFK3B7OvceiFfOC5Ge7UBO6iyxFaibc6q1lN2YOKgGyRxh62hOdN1xLbaTx+MOkCTkqOVYjQqNnhst6/Zs+LioAlbo6O+nAM4aNLyVy+bzdCsk+XK9+PNElm2b1txx16SyNKd/U3tTUJTeUNSJL1faPoemmnUT83R8yn9DuCxp6sO7l0PxcuuB3/a0Uj6pKPgravrOE530oFppTLJVk2AZwkqyZ5UZdyjepnwqlGr6npVbWhk1apVCfoNtEO7SNpuAb8AAAAASUVORK5CYII=">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="TikTok Live Tracker">
        <meta name="twitter:description" content="Track any TikTok user's livestream instantly. Just enter their @username and go.">
        <meta name="twitter:image" content="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAFQAAABUCAMAAAArteDzAAAA/FBMVEUAAAAl8+3/KFj+LFUn8+//MFgl9e4l9O7+LFX+LVX/MFAl9O4l9O79LFYg+e8l9O7+LVX/LFQo9+/9Llf/LVP9K1X9LFQn9e7/K1YHBwf+LFb9LVT/LVYn9O8m9O7+LVYl9O7+LFUm9e79LFUk8u7/MFUAAAAg0cy4MU5tEyUlBw1uXGrWJkkm8+7/LFYcubMg0c0Yop//LVYAAAAl9O7+LFUFHx5gESAg1dB/Fiu+IUCeGzUj5d/uKVBvEyUSenfeJ0uPGTAQAwUJPTwHLi1ACxUCDw8gBgsXmJWuHjsUiYYOXFoexsEbsq3OJEVPDhowCBEQa2gLTEsMTUvQL1NGAAAAM3RSTlMAYCC/QCCfgO9gEL+PgBDv30AgcFCfgG8w6q+gjzDf38/Pr5BQMCDf39/fz8+vgH9wYFAMSP/SAAAC50lEQVRYw+3YZ3eiQBSA4YkUgQjWGNPrZnuFGyEI1liSTd3//192YDF3jG5AmJz9sL5fPfOcCygMkn+aIWJ7vNA1C5NfA13nidpRb3O5HDf03IzyAHLc0clroE2AH9zRngc6d9R0QOCP9jqgcUfNS9C5oZivaNxQzNnliKJa5odiH874odj51wo/FFVbFnmh2HBMb4THcoULij3a9AORE4r1xzZHFHu3vatyRy8AhP8HvWy+AtoEd5QONU43C7IsF8RFKCCbHDXy61ZUfjH65WBytxQqBmIMmpN2wJs07xOiFSRbN1ftjxu7+iKUEMrSbi9ctxOHrhUj8eYhRBwAWIxS9uQAwmLQQkSO+yYtBqU1dEGJQ+VoSro+DsW0ck2txZmtBxOLQePLh6Z9bnJCcWP7E00OaGUf5+SGymhyQytWUB+5e991Xd9Ni+KgV7gFu4CoDKgRHvxwanYBsqF46a+n5i2EKRtVVa1up0Y/B4POzqnUNRKVEg3uI+3pJQpNQSIkG7pnMavCa0QPOisqBr+laNEIaCUtC4qLxtGiO6DVSBYU7yW/mB+RQDihfQZVeaCbzCIfABrZUFyEL0kA848ZRJ2k6CmD9rx59JBBu0lRg0FNF0Bb8DUe4tc4GUr2mV3dyIPy/C2sxfyHADEanrQBviSCPncZrTFzypVkqIj3KJpTmn949fGUJt45FukdGvPV2TnxR2x2KHqS+Plsm0xb342Z7VqL/ZZCOSFqFK3B7OvceiFfOC5Ge7UBO6iyxFaibc6q1lN2YOKgGyRxh62hOdN1xLbaTx+MOkCTkqOVYjQqNnhst6/Zs+LioAlbo6O+nAM4aNLyVy+bzdCsk+XK9+PNElm2b1txx16SyNKd/U3tTUJTeUNSJL1faPoemmnUT83R8yn9DuCxp6sO7l0PxcuuB3/a0Uj6pKPgravrOE530oFppTLJVk2AZwkqyZ5UZdyjepnwqlGr6npVbWhk1apVCfoNtEO7SNpuAb8AAAAASUVORK5CYII=">
        <style>
            body {
                font-family: 'Inter', sans-serif;
            }
            .bg-gradient-radial {
                background-image: radial-gradient(circle at top left, #1e3a8a, #111827 25%);
            }
        </style>
    </head>
    <body class="bg-zinc-900 text-white flex items-center justify-center min-h-screen bg-gradient-radial">
        <div class="w-full max-w-lg p-8 rounded-2xl border border-zinc-700 bg-zinc-900/60 backdrop-blur-sm shadow-2xl">
            <div class="flex justify-center mb-6">
                <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAFQAAABUCAMAAAArteDzAAAA/FBMVEUAAAAl8+3/KFj+LFUn8+//MFgl9e4l9O7+LFX+LVX/MFAl9O4l9O79LFYg+e8l9O7+LVX/LFQo9+/9Llf/LVP9K1X9LFQn9e7/K1YHBwf+LFb9LVT/LVYn9O8m9O7+LVYl9O7+LFUm9e79LFUk8u7/MFUAAAAg0cy4MU5tEyUlBw1uXGrWJkkm8+7/LFYcubMg0c0Yop//LVYAAAAl9O7+LFUFHx5gESAg1dB/Fiu+IUCeGzUj5d/uKVBvEyUSenfeJ0uPGTAQAwUJPTwHLi1ACxUCDw8gBgsXmJWuHjsUiYYOXFoexsEbsq3OJEVPDhowCBEQa2gLTEsMTUvQL1NGAAAAM3RSTlMAYCC/QCCfgO9gEL+PgBDv30AgcFCfgG8w6q+gjzDf38/Pr5BQMCDf39/fz8+vgH9wYFAMSP/SAAAC50lEQVRYw+3YZ3eiQBSA4YkUgQjWGNPrZnuFGyEI1liSTd3//192YDF3jG5AmJz9sL5fPfOcCygMkn+aIWJ7vNA1C5NfA13nidpRb3O5HDf03IzyAHLc0clroE2AH9zRngc6d9R0QOCP9jqgcUfNS9C5oZivaNxQzNnliKJa5odiH874odj51wo/FFVbFnmh2HBMb4THcoULij3a9AORE4r1xzZHFHu3vatyRy8AhP8HvWy+AtoEd5QONU43C7IsF8RFKCCbHDXy61ZUfjH65WBytxQqBmIMmpN2wJs07xOiFSRbN1ftjxu7+iKUEMrSbi9ctxOHrhUj8eYhRBwAWIxS9uQAwmLQQkSO+yYtBqU1dEGJQ+VoSro+DsW0ck2txZmtBxOLQePLh6Z9bnJCcWP7E00OaGUf5+SGymhyQytWUB+5e991Xd9Ni+KgV7gFu4CoDKgRHvxwanYBsqF46a+n5i2EKRtVVa1up0Y/B4POzqnUNRKVEg3uI+3pJQpNQSIkG7pnMavCa0QPOisqBr+laNEIaCUtC4qLxtGiO6DVSBYU7yW/mB+RQDihfQZVeaCbzCIfABrZUFyEL0kA848ZRJ2k6CmD9rx59JBBu0lRg0FNF0Bb8DUe4tc4GUr2mV3dyIPy/C2sxfyHADEanrQBviSCPncZrTFzypVkqIj3KJpTmn949fGUJt45FukdGvPV2TnxR2x2KHqS+Plsm0xb342Z7VqL/ZZCOSFqFK3B7OvceiFfOC5Ge7UBO6iyxFaibc6q1lN2YOKgGyRxh62hOdN1xLbaTx+MOkCTkqOVYjQqNnhst6/Zs+LioAlbo6O+nAM4aNLyVy+bzdCsk+XK9+PNElm2b1txx16SyNKd/U3tTUJTeUNSJL1faPoemmnUT83R8yn9DuCxp6sO7l0PxcuuB3/a0Uj6pKPgravrOE530oFppTLJVk2AZwkqyZ5UZdyjepnwqlGr6npVbWhk1apVCfoNtEO7SNpuAb8AAAAASUVORK5CYII=" class="h-16 w-16" alt="TikTok">
            </div>
            <h1 class="text-4xl font-extrabold mb-3 text-transparent bg-clip-text bg-gradient-to-r from-gray-200 to-gray-400 text-center">TikTok Live</h1>
            <p class="text-lg text-gray-400 mb-8 text-center">Enter a TikTok username below to start tracking their live stream.</p>
            <form id="user-form" class="flex flex-col sm:flex-row gap-3">
                <div class="relative flex-grow">
                    <span class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400">@</span>
                    <input type="text" id="username-input" placeholder="username" class="w-full bg-zinc-800 border border-zinc-700 text-white rounded-lg pl-8 pr-4 py-3 focus:outline-none focus:ring-2 focus:ring-sky-500 transition-all">
                </div>
                <button type="submit" class="bg-white hover:bg-gray-100 text-black font-bold py-3 px-6 rounded-lg transition-all flex items-center justify-center gap-2">
                    <i class="fa-solid fa-magnifying-glass"></i>
                    <span>Track User</span>
                </button>
            </form>
            <p class="text-xs text-zinc-500 mt-6 text-center">Alternatively, navigate directly via URL: <code class="bg-zinc-700 p-1 rounded-md">/username</code></p>
        </div>
        <script>
            document.getElementById('user-form').addEventListener('submit', function(event) {
                event.preventDefault();
                const username = document.getElementById('username-input').value.trim();
                if (username) {
                    window.location.href = `/${username}`;
                }
            });
        </script>
    </body>
    </html>
    """)

@app.get("/{username}")
async def get_overlay_for_user(username: str):
    try:
        with open("overlay.html", "r", encoding="utf-8") as f:
            html_content = f.read()

        profile_data = await get_user_profile_from_web(username)

        if profile_data:
            title = escape(f"{profile_data.get('nickname', username)}'s Live | TikTok API Tracker")
            description = escape(profile_data.get('bio', "Track any TikTok user's livestream instantly."))
            icon = escape(profile_data.get('avatar', ''))

            html_content = html_content.replace("__PAGE_TITLE__", title)
            html_content = html_content.replace("__PAGE_DESCRIPTION__", description)
            html_content = html_content.replace("__PAGE_ICON__", icon)
        else:
            html_content = html_content.replace("__PAGE_TITLE__", f"@{username} | TikTok API Tracker")
            html_content = html_content.replace("__PAGE_DESCRIPTION__", "Track any TikTok user's livestream instantly.")
            html_content = html_content.replace("__PAGE_ICON__", "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAFQAAABUCAMAAAArteDzAAAA/FBMVEUAAAAl8+3/KFj+LFUn8+//MFgl9e4l9O7+LFX+LVX/MFAl9O4l9O79LFYg+e8l9O7+LVX/LFQo9+/9Llf/LVP9K1X9LFQn9e7/K1YHBwf+LFb9LVT/LVYn9O8m9O7+LVYl9O7+LFUm9e79LFUk8u7/MFUAAAAg0cy4MU5tEyUlBw1uXGrWJkkm8+7/LFYcubMg0c0Yop//LVYAAAAl9O7+LFUFHx5gESAg1dB/Fiu+IUCeGzUj5d/uKVBvEyUSenfeJ0uPGTAQAwUJPTwHLi1ACxUCDw8gBgsXmJWuHjsUiYYOXFoexsEbsq3OJEVPDhowCBEQa2gLTEsMTUvQL1NGAAAAM3RSTlMAYCC/QCCfgO9gEL+PgBDv30AgcFCfgG8w6q+gjzDf38/Pr5BQMCDf39/fz8+vgH9wYFAMSP/SAAAC50lEQVRYw+3YZ3eiQBSA4YkUgQjWGNPrZnuFGyEI1liSTd3//192YDF3jG5AmJz9sL5fPfOcCygMkn+aIWJ7vNA1C5NfA13nidpRb3O5HDf03IzyAHLc0clroE2AH9zRngc6d9R0QOCP9jqgcUfNS9C5oZivaNxQzNnliKJa5odiH874odj51wo/FFVbFnmh2HBMb4THcoULij3a9AORE4r1xzZHFHu3vatyRy8AhP8HvWy+AtoEd5QONU43C7IsF8RFKCCbHDXy61ZUfjH65WBytxQqBmIMmpN2wJs07xOiFSRbN1ftjxu7+iKUEMrSbi9ctxOHrhUj8eYhRBwAWIxS9uQAwmLQQkSO+yYtBqU1dEGJQ+VoSro+DsW0ck2txZmtBxOLQePLh6Z9bnJCcWP7E00OaGUf5+SGymhyQytWUB+5e991Xd9Ni+KgV7gFu4CoDKgRHvxwanYBsqF46a+n5i2EKRtVVa1up0Y/B4POzqnUNRKVEg3uI+3pJQpNQSIkG7pnMavCa0QPOisqBr+laNEIaCUtC4qLxtGiO6DVSBYU7yW/mB+RQDihfQZVeaCbzCIfABrZUFyEL0kA848ZRJ2k6CmD9rx59JBBu0lRg0FNF0Bb8DUe4tc4GUr2mV3dyIPy/C2sxfyHADEanrQBviSCPncZrTFzypVkqIj3KJpTmn949fGUJt45FukdGvPV2TnxR2x2KHqS+Plsm0xb342Z7VqL/ZZCOSFqFK3B7OvceiFfOC5Ge7UBO6iyxFaibc6q1lN2YOKgGyRxh62hOdN1xLbaTx+MOkCTkqOVYjQqNnhst6/Zs+LioAlbo6O+nAM4aNLyVy+bzdCsk+XK9+PNElm2b1txx16SyNKd/U3tTUJTeUNSJL1faPoemmnUT83R8yn9DuCxp6sO7l0PxcuuB3/a0Uj6pKPgravrOE530oFppTLJVk2AZwkqyZ5UZdyjepnwqlGr6npVbWhk1apVCfoNtEO7SNpuAb8AAAAASUVORK5CYII=")

        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: overlay.html not found.</h1>", status_code=500)

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
    from uvicorn.config import LOGGING_CONFIG
    LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelname)s - %(message)s"
    uvicorn.run(app, host="0.0.0.0", port=8000)
