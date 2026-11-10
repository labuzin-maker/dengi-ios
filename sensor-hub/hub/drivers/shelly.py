"""Wi-Fi устройства Shelly: работают по локальной сети без облака и без ключей."""
from __future__ import annotations

import asyncio

import aiohttp

from ..core import Driver


class ShellyDriver(Driver):
    name = "shelly"
    title = "Shelly (Wi-Fi)"

    async def run(self):
        hosts = self.cfg.get("hosts") or []
        self.poll = max(5, int(self.cfg.get("poll_seconds", 15)))
        self.gen = {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as self.http:
            while True:
                ok = 0
                for host in hosts:
                    try:
                        await self._poll(host)
                        ok += 1
                    except Exception:
                        d = self.hub.devices.get("shelly:" + host)
                        if d and d.online:
                            d.online = False
                            self.hub.notify()
                self.set_status("ok" if ok or not hosts else "error",
                                f"На связи: {ok} из {len(hosts)}")
                await asyncio.sleep(self.poll)

    async def _get(self, host, path):
        async with self.http.get(f"http://{host}{path}") as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def _poll(self, host):
        dev_id = "shelly:" + host
        if host not in self.gen:
            info = await self._get(host, "/shelly")
            self.gen[host] = (info.get("gen") or 1, info.get("name") or info.get("app")
                              or info.get("type") or "Shelly", info.get("model") or info.get("type") or "")
        gen, name, model = self.gen[host]
        self.hub.device(dev_id, name=name, driver=self.name, model=model, online=True)
        up = lambda eid, key, v, **kw: v is not None and self.hub.update(dev_id, eid, key, v, **kw)
        if gen >= 2:
            st = await self._get(host, "/rpc/Shelly.GetStatus")
            for k, v in st.items():
                comp, _, idx = k.partition(":")
                sfx = "" if idx in ("", "0") else f"_{int(idx) + 1}"
                if comp == "switch":
                    up("switch" + sfx, "switch", v.get("output"), writable=True)
                    up("power" + sfx, "power", v.get("apower"))
                elif comp == "temperature":
                    up("temperature" + sfx, "temperature", v.get("tC"))
                elif comp == "humidity":
                    up("humidity" + sfx, "humidity", v.get("rh"))
                elif comp == "devicepower":
                    up("battery", "battery", (v.get("battery") or {}).get("percent"))
                elif comp == "input" and "state" in v and v["state"] is not None:
                    pass
        else:
            st = await self._get(host, "/status")
            for i, r in enumerate(st.get("relays") or []):
                up("switch" + ("" if i == 0 else f"_{i + 1}"), "switch", r.get("ison"), writable=True)
            for i, m in enumerate(st.get("meters") or []):
                up("power" + ("" if i == 0 else f"_{i + 1}"), "power", m.get("power"))
            up("temperature", "temperature", (st.get("tmp") or {}).get("tC"))
            up("humidity", "humidity", (st.get("hum") or {}).get("value"))
            up("battery", "battery", (st.get("bat") or {}).get("value"))

    async def set(self, device, entity, value):
        host = device.id.split(":", 1)[1]
        eid = entity.id.rsplit("/", 1)[1]
        idx = int(eid.split("_")[1]) - 1 if "_" in eid else 0
        on = "true" if value else "false"
        if self.gen.get(host, (1,))[0] >= 2:
            await self._get(host, f"/rpc/Switch.Set?id={idx}&on={on}")
        else:
            await self._get(host, f"/relay/{idx}?turn={'on' if value else 'off'}")
        self.hub.update(device.id, eid, "switch", bool(value), writable=True)
