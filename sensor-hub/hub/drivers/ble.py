"""Прямой приём Bluetooth-датчиков адаптером компьютера (без хаба Xiaomi).

Нужен Bluetooth на компьютере с хабом и пакет bleak. Датчики не «подключаются»:
хаб слушает эфир и показывает всё, что передаёт данные в открытом виде.
"""
from __future__ import annotations

import asyncio
import time

from ..core import Driver
from .ble_parsers import parse_advert


class BleDriver(Driver):
    name = "ble"
    title = "Bluetooth напрямую"

    async def run(self):
        try:
            from bleak import BleakScanner
        except ImportError:
            self.set_status("error", "Не установлен пакет bleak (pip install bleak)")
            await asyncio.Event().wait()
        seen, encrypted = {}, set()

        def on_adv(device, adv):
            fmt, vals = parse_advert(adv.service_data or {})
            if not vals:
                return
            mac = device.address.upper()
            if vals.get("_encrypted"):
                encrypted.add(mac)
                return
            dev_id = "ble:" + mac
            name = adv.local_name or device.name or f"{fmt} {mac[-5:]}"
            self.hub.device(dev_id, name=name, driver=self.name, model=fmt, online=True)
            for key, val in vals.items():
                self.hub.update(dev_id, key, key, val)
            seen[dev_id] = time.time()

        while True:
            try:
                async with BleakScanner(on_adv):
                    while True:
                        await asyncio.sleep(30)
                        now = time.time()
                        for dev_id, ts in seen.items():
                            d = self.hub.devices.get(dev_id)
                            if d and d.online and now - ts > 1800:
                                d.online = False
                                self.hub.notify()
                        msg = f"Датчиков в эфире: {len(seen)}"
                        if encrypted:
                            msg += f"; с шифрованием (нужен хаб Xiaomi): {len(encrypted)}"
                        self.set_status("ok", msg)
            except Exception as e:
                self.set_status("error", f"Bluetooth недоступен: {e}"[:200])
                await asyncio.sleep(30)
