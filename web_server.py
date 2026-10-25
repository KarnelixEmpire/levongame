"""
Máy chủ chơi online trên web: Mạt chược và Cò quay Nga, đăng nhập bằng Discord.

Chạy:
    python web_server.py

Biến môi trường:
    PORT                  cổng chạy, mặc định 8080
    PUBLIC_URL            địa chỉ công khai của trang, ví dụ http://localhost:8080
    DISCORD_CLIENT_ID     Client ID của ứng dụng Discord (mặc định là ID của bot Levon Games)
    DISCORD_CLIENT_SECRET Client Secret lấy trong Discord Developer Portal (bắt buộc để đăng nhập)

Dùng aiohttp, đã có sẵn khi cài discord.py.
"""
import logging
import os
import random
import secrets
import string
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

from mahjong_engine import GameError, Room
from roulette_engine import RouletteRoom

BASE = Path(__file__).resolve().parent
INDEX = BASE / "index.html"
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "1557745642841968723")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = PUBLIC_URL + "/auth/callback"
FRONT_URL = os.environ.get("FRONT_URL", PUBLIC_URL).rstrip("/")  # trang web gọi máy chủ này, ví dụ https://levon-game.vercel.app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web")

ROOMS: dict = {}     # mã bàn -> Room hoặc RouletteRoom
CONN: dict = {}      # WebSocket -> (pid, mã bàn)
SESSIONS: dict = {}  # mã phiên -> {"pid", "name"}


# =========================================================
# Bàn chơi
# =========================================================
def new_code() -> str:
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in ROOMS:
            return code


def bind(ws, pid: str, room) -> None:
    room.player(pid).ws = ws
    CONN[ws] = (pid, room.code)


def unbind(ws) -> None:
    info = CONN.pop(ws, None)
    if not info:
        return
    pid, code = info
    room = ROOMS.get(code)
    if not room:
        return
    p = room.player(pid)
    if p and p.ws is ws:
        p.ws = None
    # Bàn không còn ai kết nối và không đang chơi thì xóa bàn
    if not any(pl.online for pl in room.players) and room.state != "playing":
        ROOMS.pop(code, None)


async def push(room) -> None:
    for p in room.players:
        if p.ws is not None and not p.ws.closed:
            await p.ws.send_json({"type": "state", "room": room.view(p.pid)})


def _room_of(ws):
    info = CONN.get(ws)
    if not info:
        raise GameError("Bạn chưa vào bàn nào.")
    room = ROOMS.get(info[1])
    if not room:
        raise GameError("Bàn đã đóng.")
    return room


async def handle(ws, msg: dict, ident: dict) -> None:
    kind = msg.get("type")
    pid = ident["pid"]
    name = ident["name"] or str(msg.get("name", "")).strip()[:20]

    if kind == "create":
        if not name:
            raise GameError("Hãy nhập tên của bạn.")
        game = msg.get("game", "mahjong")
        team_mode = bool(msg.get("team_mode", False))
        code = new_code()
        if game == "russian":
            room = RouletteRoom(code, pid, int(msg.get("bullets", 1)), int(msg.get("bet", 10000)), team_mode)
        elif game == "mahjong":
            room = Room(code, pid, team_mode)
        else:
            raise GameError("Trò chơi không hợp lệ.")
        ROOMS[code] = room
        room.add(pid, name)
        bind(ws, pid, room)

    elif kind == "join":
        code = str(msg.get("code", "")).strip().upper()
        room = ROOMS.get(code)
        if not room:
            raise GameError("Không tìm thấy bàn với mã này.")
        if room.player(pid):
            bind(ws, pid, room)              # quay lại bàn cũ, ví dụ sau khi mất mạng
        else:
            if not name:
                raise GameError("Hãy nhập tên của bạn.")
            room.add(pid, name)
            bind(ws, pid, room)

    elif kind == "leave":
        room = _room_of(ws)
        room.remove(pid)
        CONN.pop(ws, None)
        if not room.players:
            ROOMS.pop(room.code, None)
            return
        await push(room)
        return

    else:
        room = _room_of(ws)
        if kind == "start":
            room.start(pid)
        elif kind == "team":
            room.set_team(pid, msg.get("team"))
        elif kind == "draw":
            if room.kind != "mahjong":
                raise GameError("Bàn này không có thao tác rút bài.")
            room.draw(pid)
        elif kind == "discard":
            if room.kind != "mahjong":
                raise GameError("Bàn này không có thao tác đánh bài.")
            room.discard(pid, int(msg.get("idx", -1)))
        elif kind == "pull":
            if room.kind != "russian":
                raise GameError("Bàn này không có thao tác bấm cò.")
            room.pull(pid)
            if room.state == "ended":
                _finish(room)
        elif kind == "end":
            if room.kind != "russian":
                raise GameError("Bàn này không có thao tác kết thúc sớm.")
            room.end_early(pid)
            _finish(room)
        else:
            raise GameError("Yêu cầu không hợp lệ.")

    await push(_room_of(ws))


def _finish(room) -> None:
    """Ván kết thúc: bàn vẫn còn để mọi người xem kết quả và bắt đầu ván mới."""
    log.info("Bàn %s kết thúc: %s", room.code, room.result)


# =========================================================
# Đăng nhập Discord (OAuth2, scope identify)
# =========================================================
def _sid_from(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query.get("sid", "")


def _session(request: web.Request):
    sid = _sid_from(request)
    return SESSIONS.get(sid) if sid else None


@web.middleware
async def cors_mw(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = FRONT_URL
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def auth_login(request: web.Request):
    if not DISCORD_CLIENT_SECRET:
        return web.Response(
            text="Máy chủ chưa cấu hình đăng nhập Discord (thiếu DISCORD_CLIENT_SECRET).",
            status=500,
        )
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    resp = web.HTTPFound("https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(params))
    resp.set_cookie("levon_state", state, max_age=600, httponly=True, samesite="Lax")
    raise resp


async def auth_callback(request: web.Request):
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    if not code or not state or state != request.cookies.get("levon_state"):
        return web.Response(text="Đăng nhập không hợp lệ. Hãy thử lại từ trang chơi online.", status=400)
    async with aiohttp.ClientSession() as cs:
        async with cs.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        ) as r:
            if r.status != 200:
                return web.Response(text="Không lấy được token từ Discord. Kiểm tra lại Client Secret.", status=400)
            token = (await r.json())["access_token"]
        async with cs.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": "Bearer " + token},
        ) as r:
            if r.status != 200:
                return web.Response(text="Không đọc được thông tin tài khoản Discord.", status=400)
            me = await r.json()
    name = (me.get("global_name") or me.get("username") or "Người chơi")[:20]
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = {"pid": "d:" + str(me["id"]), "name": name}
    resp = web.HTTPFound(f"{FRONT_URL}/#/online?sid={sid}")
    resp.del_cookie("levon_state")
    raise resp


async def auth_me(request: web.Request):
    s = _session(request)
    return web.json_response({"logged_in": bool(s), "name": s["name"] if s else ""})


async def auth_logout(request: web.Request):
    SESSIONS.pop(_sid_from(request), None)
    return web.json_response({"ok": True})


# =========================================================
# Trang và WebSocket
# =========================================================
async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(INDEX)


async def ws_handler(request: web.Request) -> web.StreamResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    s = _session(request)
    guest = request.query.get("token", "")[:64]
    if s:
        ident = {"pid": s["pid"], "name": s["name"]}
    elif guest:
        ident = {"pid": "g:" + guest, "name": None}
    else:
        ident = None

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                if ident is None:
                    raise GameError("Thiếu mã người chơi, hãy tải lại trang.")
                data = msg.json()
                if not isinstance(data, dict):
                    raise GameError("Dữ liệu không hợp lệ.")
                await handle(ws, data, ident)
            except GameError as e:
                await ws.send_json({"type": "error", "msg": str(e)})
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                await ws.send_json({"type": "error", "msg": "Dữ liệu không hợp lệ."})
    finally:
        unbind(ws)
    return ws


def make_app() -> web.Application:
    app = web.Application(middlewares=[cors_mw])
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/auth/login", auth_login)
    app.router.add_get("/auth/callback", auth_callback)
    app.router.add_get("/auth/me", auth_me)
    app.router.add_post("/auth/logout", auth_logout)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=PORT)
