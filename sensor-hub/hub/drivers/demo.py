"""Демо-датчики: показываются, пока не подключено ни одного настоящего."""
from __future__ import annotations

import asyncio
import math
import random
import time

from ..core import Driver

DEVICES = [
    ("demo:living", "Термометр", "Гостиная", "temperature-humidity-sensor", {"temperature": 22.4, "humidity": 41, "battery": 87}),
    ("demo:kids", "Термометр", "Детская", "temperature-humidity-sensor", {"temperature": 23.1, "humidity": 44, "battery": 62}),
    ("demo:balcony", "Уличный термометр", "Балкон", "temperature-humidity-sensor", {"temperature": -3.5, "humidity": 71, "battery": 34}),
    ("demo:door", "Входная дверь", "Прихожая", "magnet-sensor", {"contact": False, "battery": 95}),
    ("demo:hall", "Датчик движения", "Прихожая", "motion-sensor", {"motion": False, "illuminance": 120}),
    ("demo:bath", "Датчик протечки", "Ванная", "submersion-sensor", {"leak": False, "battery": 78}),
    ("demo:plug", "Розетка обогревателя", "Детская", "outlet", {"switch": True, "power": 840.0}),
]


class DemoDriver(Driver):
    name = "demo"
    title = "Демо-датчики"

    async def run(self):
        mode = self.cfg.get("mode", "auto")
        vals = self.vals = {i: dict(v) for i, _, _, _, v in DEVICES}
        while True:
            if mode == "off" or (mode == "auto" and self.hub.has_real_devices()):
                self.hub.remove_driver_devices(self.name)
                self.set_status("ok", "Скрыты: подключены настоящие датчики")
                await asyncio.sleep(10)
                continue
            self.set_status("ok", "Примеры, пока нет настоящих датчиков")
            t = time.time()
            for dev_id, name, room, cat, _ in DEVICES:
                self.hub.device(dev_id, name=name, driver=self.name, model="Пример", room=room, category=cat)
                v = vals[dev_id]
                for key in v:
                    if key == "temperature":
                        v[key] = round(v[key] + random.uniform(-0.1, 0.1) + 0.02 * math.sin(t / 600), 1)
                    elif key == "humidity":
                        v[key] = max(20, min(90, v[key] + random.choice((-1, 0, 0, 0, 1))))
                    elif key == "motion":
                        v[key] = random.random() < 0.15
                    elif key == "illuminance":
                        v[key] = max(0, v[key] + random.randint(-8, 8))
                    elif key == "power":
                        v[key] = round(840 + random.uniform(-15, 15), 1) if v["switch"] else 0.0
                    self.hub.update(dev_id, key, key, v[key], writable=(key == "switch"))
            await asyncio.sleep(5)

    async def set(self, device, entity, value):
        eid = entity.id.rsplit("/", 1)[1]
        self.vals[device.id][eid] = bool(value)
        self.hub.update(device.id, eid, "switch", bool(value), writable=True)
        if "power" in device.entities:
            self.hub.update(device.id, "power", "power", 840.0 if value else 0.0)
