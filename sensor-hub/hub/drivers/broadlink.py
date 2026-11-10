"""Wi-Fi термостаты тёплого пола на модуле Broadlink (Beok, Floureon, Hysen и похожие).

Работают только по домашней сети, без облака: хаб должен стоять дома, в той же сети Wi-Fi.
"""
from __future__ import annotations

import asyncio
import time

from ..core import Driver


def _lib():
    try:
        import broadlink
        return broadlink
    except ImportError:
        raise RuntimeError("Не установлена библиотека: выполните pip install broadlink") from None


class BroadlinkDriver(Driver):
    name = "broadlink"
    title = "Тёплые полы (Wi-Fi, Broadlink)"

    async def run(self):
        self.poll = max(10, int(self.cfg.get("poll_seconds", 30)))
        self.hosts = self.cfg.get("hosts") or []     # пусто = искать в сети самим
        self.devs = {}                                # mac -> устройство библиотеки
        self.lock = asyncio.Lock()                    # термостат отвечает на один запрос за раз
        found_at = 0.0
        while True:
            if not self.devs or time.time() - found_at > 300:
                found_at = time.time()
                try:
                    await asyncio.to_thread(self._discover)
                except Exception as e:
                    if not self.devs:
                        self.set_status("error", f"Поиск термостатов: {type(e).__name__}: {e}"[:200])
            ok = 0
            for mac, dev in list(self.devs.items()):
                try:
                    async with self.lock:
                        st = await asyncio.to_thread(dev.get_full_status)
                    self._publish(mac, dev, st)
                    ok += 1
                except Exception:
                    d = self.hub.devices.get("broadlink:" + mac)
                    if d and d.online:
                        d.online = False
                        self.hub.notify()
                    try:                              # после перезагрузки термостата нужен новый вход
                        await asyncio.to_thread(dev.auth)
                    except Exception:
                        pass
            if self.devs:
                self.set_status("ok" if ok else "error", f"На связи: {ok} из {len(self.devs)}")
            elif self.status != "error":
                self.set_status("ok", "Термостаты в домашней сети не найдены")
            await asyncio.sleep(self.poll)

    def _discover(self):
        bl = _lib()
        if self.hosts:
            found = []
            for h in self.hosts:
                try:
                    found.append(bl.hello(h, timeout=5))
                except Exception:
                    pass
        else:
            found = bl.discover(timeout=5)
        for dev in found:
            if not hasattr(dev, "get_full_status"):   # пульты и розетки Broadlink здесь не нужны
                continue
            mac = dev.mac.hex(":") if isinstance(dev.mac, (bytes, bytearray)) else str(dev.mac)
            if mac not in self.devs:
                dev.auth()
                self.devs[mac] = dev

    def _publish(self, mac, dev, st):
        dev_id = "broadlink:" + mac
        self.hub.device(dev_id, name=(getattr(dev, "name", "") or "Тёплый пол").strip(), driver=self.name,
                        model=getattr(dev, "model", "") or "Термостат", category="thermostat", online=True)
        up = self.hub.update
        # sensor: 0 — датчик в корпусе, 1 — датчик пола, 2 — оба (регулирует по воздуху, пол ограничивает)
        floor = st.get("sensor") in (1, 2) and st.get("external_temp") not in (None, 0)
        up(dev_id, "temperature", "temperature", st["external_temp"] if st.get("sensor") == 1 and floor else st["room_temp"],
           name="Температура пола" if st.get("sensor") == 1 and floor else "Температура")
        if st.get("sensor") == 2 and floor:
            up(dev_id, "floor", "temperature", st["external_temp"], name="Температура пола")
        up(dev_id, "switch", "switch", st["power"], writable=True)
        lo, hi = st.get("svl") or 5, st.get("svh") or 35
        up(dev_id, "target_temperature", "target_temperature", st["thermostat_temp"], writable=True,
           opts={"min": lo, "max": hi, "step": 0.5})
        up(dev_id, "heating", "heating", bool(st.get("active")) and bool(st["power"]))

    async def set(self, device, entity, value):
        mac = device.id.split(":", 1)[1]
        dev = self.devs.get(mac)
        if not dev:
            raise RuntimeError("Термостат не на связи")
        eid = entity.id.rsplit("/", 1)[1]
        async with self.lock:
            if eid == "switch":
                await asyncio.to_thread(dev.set_power, 1 if value else 0)
            elif eid == "target_temperature":
                o = entity.opts or {}
                value = max(o.get("min", 5), min(o.get("max", 35), round(float(value) * 2) / 2))
                await asyncio.to_thread(dev.set_temp, value)
            else:
                raise RuntimeError("Эта настройка не управляется")
            st = await asyncio.to_thread(dev.get_full_status)
        self._publish(mac, dev, st)
