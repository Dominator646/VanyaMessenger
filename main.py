import asyncio
import json
import logging
import uuid
import hashlib
import os
import http
from datetime import datetime
from typing import Dict, Set, Optional
import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory storage (для продакшена заменить на Redis/PostgreSQL)
users: Dict[str, dict] = {}          # username -> {password_hash, id}
online_users: Dict[str, WebSocketServerProtocol] = {}  # username -> ws
channels: Dict[str, dict] = {        # channel_id -> {name, members, messages}
    "general": {"name": "# general", "members": set(), "messages": []},
    "random":  {"name": "# random",  "members": set(), "messages": []},
}
voice_rooms: Dict[str, Set[str]] = {}  # room_id -> set of usernames
dm_history: Dict[str, list] = {}       # "user1:user2" -> [messages]

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def dm_key(a: str, b: str) -> str:
    return ":".join(sorted([a, b]))

async def broadcast_to_channel(channel_id: str, message: dict, exclude: str = None):
    dead = []
    for username, ws in online_users.items():
        if username == exclude:
            continue
        try:
            await ws.send(json.dumps(message))
        except:
            dead.append(username)
    for u in dead:
        online_users.pop(u, None)

async def broadcast_user_list():
    msg = {"type": "user_list", "users": list(online_users.keys())}
    await broadcast_to_channel("__all__", msg)

async def handle_register(ws, data):
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        await ws.send(json.dumps({"type": "error", "msg": "Заполните все поля"}))
        return False
    if username in users:
        await ws.send(json.dumps({"type": "error", "msg": "Пользователь уже существует"}))
        return False
    users[username] = {"password_hash": hash_password(password), "id": str(uuid.uuid4())}
    await ws.send(json.dumps({"type": "registered", "username": username}))
    return True

async def handle_login(ws, data, state):
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if username not in users:
        await ws.send(json.dumps({"type": "error", "msg": "Пользователь не найден"}))
        return False
    if users[username]["password_hash"] != hash_password(password):
        await ws.send(json.dumps({"type": "error", "msg": "Неверный пароль"}))
        return False
    state["username"] = username
    online_users[username] = ws
    channel_list = [{"id": cid, "name": ch["name"]} for cid, ch in channels.items()]
    history = {cid: ch["messages"][-50:] for cid, ch in channels.items()}
    await ws.send(json.dumps({
        "type": "login_ok",
        "username": username,
        "channels": channel_list,
        "history": history,
        "voice_rooms": {r: list(m) for r, m in voice_rooms.items()}
    }))
    await broadcast_user_list()
    logger.info(f"[+] {username} вошёл")
    return True

async def handle_message(ws, data, state):
    username = state.get("username")
    if not username:
        return
    channel_id = data.get("channel", "general")
    text = data.get("text", "").strip()
    if not text:
        return
    msg = {
        "type": "message",
        "channel": channel_id,
        "from": username,
        "text": text,
        "ts": datetime.utcnow().isoformat()
    }
    if channel_id in channels:
        channels[channel_id]["messages"].append(msg)
        if len(channels[channel_id]["messages"]) > 200:
            channels[channel_id]["messages"] = channels[channel_id]["messages"][-200:]
    await broadcast_to_channel(channel_id, msg)

async def handle_dm(ws, data, state):
    username = state.get("username")
    target = data.get("to")
    text = data.get("text", "").strip()
    if not username or not target or not text:
        return
    key = dm_key(username, target)
    msg = {
        "type": "dm",
        "from": username,
        "to": target,
        "text": text,
        "ts": datetime.utcnow().isoformat()
    }
    dm_history.setdefault(key, []).append(msg)
    await ws.send(json.dumps(msg))
    if target in online_users:
        try:
            await online_users[target].send(json.dumps(msg))
        except:
            pass

async def handle_voice_join(ws, data, state):
    username = state.get("username")
    room_id = data.get("room", "voice-general")
    if not username:
        return
    voice_rooms.setdefault(room_id, set()).add(username)
    msg = {"type": "voice_join", "room": room_id, "user": username,
           "members": list(voice_rooms[room_id])}
    await broadcast_to_channel("__all__", msg)

async def handle_voice_leave(ws, data, state):
    username = state.get("username")
    room_id = data.get("room", "voice-general")
    if username and room_id in voice_rooms:
        voice_rooms[room_id].discard(username)
        if not voice_rooms[room_id]:
            del voice_rooms[room_id]
    msg = {"type": "voice_leave", "room": room_id, "user": username,
           "members": list(voice_rooms.get(room_id, set()))}
    await broadcast_to_channel("__all__", msg)

async def handle_signal(ws, data, state):
    username = state.get("username")
    target = data.get("target")
    if not username or not target or target not in online_users:
        return
    data["from"] = username
    try:
        await online_users[target].send(json.dumps(data))
    except:
        pass

async def handle_voice_data(ws, data, state):
    username = state.get("username")
    room_id = data.get("room")
    if not username or not room_id or room_id not in voice_rooms:
        return
    for member in voice_rooms[room_id]:
        if member != username and member in online_users:
            try:
                await online_users[member].send(json.dumps(data))
            except:
                pass

HANDLERS = {
    "register":    handle_register,
    "login":       handle_login,
    "message":     handle_message,
    "dm":          handle_dm,
    "voice_join":  handle_voice_join,
    "voice_leave": handle_voice_leave,
    "signal":      handle_signal,
    "voice_data":  handle_voice_data,
}

async def health_check(connection, request):
    """
    Render шлёт HTTP GET /health для проверки живости сервиса.
    Отвечаем 200 OK на любой не-WebSocket запрос.
    """
    if request.headers.get("Upgrade", "").lower() != "websocket":
        response = connection.respond(http.HTTPStatus.OK, "PyChat server OK\n")
        return response

async def handler(ws):
    state = {"username": None}
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
                msg_type = data.get("type")
                fn = HANDLERS.get(msg_type)
                if fn:
                    if msg_type == "register":
                        await fn(ws, data)
                    else:
                        await fn(ws, data, state)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.error(f"Ошибка обработки: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        username = state.get("username")
        if username:
            online_users.pop(username, None)
            for room_id, members in list(voice_rooms.items()):
                if username in members:
                    members.discard(username)
                    await broadcast_to_channel("__all__", {
                        "type": "voice_leave", "room": room_id, "user": username,
                        "members": list(members)
                    })
            await broadcast_user_list()
            logger.info(f"[-] {username} отключился")

async def main():
    HOST = os.getenv("HOST", "0.0.0.0")
    # Render задаёт PORT автоматически. Дефолт 8765 для локального запуска.
    PORT = int(os.getenv("PORT", 8765))
    logger.info(f"Сервер запущен на {HOST}:{PORT}")
    async with websockets.serve(
        handler, HOST, PORT,
        process_request=health_check,
        ping_interval=20,
        ping_timeout=30,
        max_size=10 * 1024 * 1024,
        compression=None,
    ):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
