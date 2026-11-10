"""Веб-сервер хаба: отдаёт панель и API для неё."""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

from aiohttp import web

from .core import Hub
from .drivers import ALL

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "docs"


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def make_app(cfg: dict, data_dir: Path) -> web.Application:
    hub = Hub(data_dir)
    key = str(cfg.get("access_key") or "")

    # панель может быть открыта с GitHub Pages, поэтому разрешаем запросы с других адресов
    cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type, X-Key",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Max-Age": "86400"}

    @web.middleware
    async def guard(request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=cors)
        resp = await _guarded(request, handler)
        if not resp.prepared:
            resp.headers.update(cors)
        return resp

    async def _guarded(request, handler):
        if key and request.path.startswith("/api/"):
            if request.headers.get("X-Key") != key and request.query.get("key") != key:
                return web.json_response({"error": "Нужен ключ доступа"}, status=401)
        try:
            return await handler(request)
        except web.HTTPException as e:
            return e
        except Exception as e:
            return web.json_response({"error": str(e) or type(e).__name__}, status=400)

    app = web.Application(middlewares=[guard])
    tasks = []

    async def state(request):
        return web.json_response(hub.snapshot())

    async def events(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache", "X-Accel-Buffering": "no", **cors})
        await resp.prepare(request)
        q = hub.subscribe()
        try:
            while True:
                await resp.write(b"data: " + json.dumps(hub.snapshot(), ensure_ascii=False).encode() + b"\n\n")
                try:
                    await asyncio.wait_for(q.get(), 25)
                    await asyncio.sleep(0.3)        # собрать пачку изменений
                    while not q.empty():
                        q.get_nowait()
                except asyncio.TimeoutError:
                    pass
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            hub.unsubscribe(q)
        return resp

    async def set_value(request):
        body = await request.json()
        dev, ent = hub.find_entity(body.get("entity", ""))
        if not ent:
            raise RuntimeError("Нет такого датчика")
        if not ent.writable:
            raise RuntimeError("Это устройство не управляется")
        if body.get("value") is None:
            raise RuntimeError("Не указано значение")
        await hub.drivers[dev.driver].set(dev, ent, body.get("value"))
        return web.json_response({"ok": True})

    async def video(request):
        body = await request.json()
        dev = hub.devices.get(body.get("id", ""))
        if not dev:
            raise RuntimeError("Нет такого устройства")
        return web.json_response({"url": await hub.drivers[dev.driver].video(dev)})

    async def edit_device(request):
        body = await request.json()
        if body.get("id") not in hub.devices:
            raise RuntimeError("Нет такого устройства")
        hub.set_override(body["id"], name=body.get("name"), room=body.get("room"),
                         hidden=body.get("hidden"))
        return web.json_response({"ok": True})

    async def history(request):
        hours = min(float(request.query.get("hours", 24)), 24 * 30)
        return web.json_response(hub.history(request.query.get("entity", ""), hours))

    async def sparks(request):
        return web.json_response(hub.sparks(min(float(request.query.get("hours", 24)), 168)))

    async def xiaomi_login(request):
        drv = hub.drivers.get("xiaomi")
        if not drv:
            raise RuntimeError("Источник Xiaomi выключен в config.yaml")
        return web.json_response(await drv.login_start())

    async def xiaomi_logout(request):
        hub.drivers["xiaomi"].logout()
        return web.json_response({"ok": True})

    async def diagnostics(request):
        drv = hub.drivers.get("xiaomi")
        return web.json_response({"xiaomi": drv.diagnostics() if drv else []},
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False, indent=1))

    async def index(request):
        return web.FileResponse(PANEL / "index.html", headers={"Cache-Control": "no-cache"})

    app.router.add_get("/api/state", state)
    app.router.add_get("/api/events", events)
    app.router.add_post("/api/set", set_value)
    app.router.add_post("/api/device", edit_device)
    app.router.add_post("/api/video", video)
    app.router.add_get("/api/history", history)
    app.router.add_get("/api/sparks", sparks)
    app.router.add_post("/api/xiaomi/login", xiaomi_login)
    app.router.add_post("/api/xiaomi/logout", xiaomi_logout)
    app.router.add_get("/api/diagnostics", diagnostics)
    app.router.add_get("/", index)
    app.router.add_static("/", PANEL)

    async def supervise(drv):
        while True:
            try:
                await drv.run()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                drv.set_status("error", f"{type(e).__name__}: {e}"[:200])
            await asyncio.sleep(15)

    async def start(app):
        sections = cfg.get("sources") or {}
        for name, cls in ALL.items():
            c = sections.get(name)
            if c is None:
                c = {} if name in ("xiaomi", "demo") else False
            if c is False or (isinstance(c, dict) and c.get("enabled") is False):
                continue
            hub.drivers[name] = cls(hub, c if isinstance(c, dict) else {})
        tasks.extend(asyncio.create_task(supervise(d)) for d in hub.drivers.values())

    async def stop(app):
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        x = hub.drivers.get("xiaomi")
        if x:
            await x.cloud.close()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app
