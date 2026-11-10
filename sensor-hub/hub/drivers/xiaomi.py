"""Драйвер Xiaomi / Mi Home.

Берёт из аккаунта Mi Home все устройства: Wi-Fi, а также Bluetooth и Zigbee,
подключённые через хаб Xiaomi. Что умеет устройство, хаб узнаёт сам
из открытого каталога miot-spec.org, поэтому новые датчики появляются
в панели без правки кода: достаточно добавить их в Mi Home.
"""
from __future__ import annotations

import asyncio
import json
import time

import aiohttp

from ..core import Driver
from .xiaomi_api import COUNTRIES, AuthError, XiaomiCloud

# имя свойства в спецификации -> наш ключ
PROP_MAP = {
    "temperature": "temperature", "relative-humidity": "humidity",
    "battery-level": "battery", "illumination": "illuminance",
    "atmospheric-pressure": "pressure", "pm2.5-density": "pm25", "pm10-density": "pm10",
    "co2-density": "co2", "tvoc-density": "tvoc", "soil-moisture": "moisture",
    "soil-ec": "conductivity", "electric-power": "power", "power-consumption": "energy",
    "voltage": "voltage", "motion-state": "motion", "contact-state": "contact",
    "submersion-state": "leak", "smoke-concentration": None,
}
# сервисы, в которых свойство "on" — не основной выключатель
SKIP_ON_SERVICES = {"indicator-light", "physical-controls-locked", "alarm", "screen", "night-light"}
# названия выключателей по имени сервиса
SWITCH_LABELS = {
    "switch": "Питание", "outlet": "Питание", "light": "Свет", "humidifier": "Увлажнитель",
    "dehumidifier": "Осушитель", "air-purifier": "Очиститель", "fan": "Вентилятор",
    "heater": "Обогрев", "air-conditioner": "Кондиционер", "camera-control": "Камера",
    "usb-switch": "USB", "germicidal-lamp": "Лампа", "television": "Телевизор",
    "vacuum": "Пылесос", "kettle": "Чайник", "night-light": "Ночник",
}
# у этих типов устройств температура и влажность — это климат в комнате,
# у остальных (розетка, камера...) температура означает нагрев самого устройства
CLIMATE_CATS = {"temperature-humidity-sensor", "temperature-sensor", "humidity-sensor", "air-monitor",
                "humidifier", "dehumidifier", "air-purifier", "air-conditioner", "heater", "thermostat",
                "fan", "environment-sensor", "air-fresh"}
SPEC_URL = "https://miot-spec.org/miot-spec-v2/"


def urn_name(urn: str) -> str:
    p = (urn or "").split(":")
    return p[3] if len(p) > 3 else ""


def map_spec(spec: dict) -> list[dict]:
    """Выбрать из спецификации устройства понятные показания и выключатели."""
    out, seen = [], {}
    category = urn_name(spec.get("type"))
    for srv in spec.get("services") or []:
        sname = urn_name(srv.get("type"))
        if sname == "device-information":
            continue
        for prop in srv.get("properties") or []:
            pname = urn_name(prop.get("type"))
            access = prop.get("access") or []
            if "read" not in access:
                continue
            item = None
            if pname == "on" and prop.get("format") == "bool" and sname not in SKIP_ON_SERVICES:
                label = SWITCH_LABELS.get(sname)
                if not label and "usb" in sname:
                    label = "USB"
                item = {"key": "switch", "writable": "write" in access,
                        "name": label or srv.get("description") or "Питание"}
            elif PROP_MAP.get(pname):
                item = {"key": PROP_MAP[pname], "writable": False, "name": None}
                unit = prop.get("unit")
                if item["key"] == "temperature" and category and category not in CLIMATE_CATS:
                    item["key"] = "device_temp"
                if item["key"] in ("temperature", "device_temp") and unit == "fahrenheit":
                    item["conv"] = "f2c"
                if item["key"] == "pressure" and unit == "pascal":
                    item["conv"] = "pa2hpa"
                if item["key"] == "energy":
                    item["unit"] = "кВт·ч" if unit == "kWh" else (unit or "")
                # состояния-перечисления: движение/контакт задаются числом
                if item["key"] in ("motion", "contact", "leak") and prop.get("format") != "bool":
                    item["conv"] = "nonzero"
            if not item:
                continue
            n = seen[item["key"]] = seen.get(item["key"], 0) + 1
            item.update(siid=srv["iid"], piid=prop["iid"],
                        eid=item["key"] if n == 1 else f"{item['key']}_{n}")
            out.append(item)
    return out


def spec_summary(spec: dict) -> list[str]:
    """Краткий перечень того, что умеет устройство (для диагностики)."""
    out = []
    for srv in spec.get("services") or []:
        sname = urn_name(srv.get("type"))
        if sname == "device-information":
            continue
        for p in srv.get("properties") or []:
            out.append(f"{srv['iid']}.{p['iid']} {sname}/{urn_name(p.get('type'))} "
                       f"{p.get('format')} {'/'.join(p.get('access') or [])}")
        for e in srv.get("events") or []:
            out.append(f"{srv['iid']}.e{e['iid']} {sname}/{urn_name(e.get('type'))} событие")
    return out


def convert(item, value):
    conv = item.get("conv")
    if value is None or conv is None:
        return value
    if conv == "f2c":
        return round((value - 32) / 1.8, 1)
    if conv == "pa2hpa":
        return round(value / 100, 1)
    if conv == "nonzero":
        return bool(value)
    return value


class XiaomiDriver(Driver):
    name = "xiaomi"
    title = "Xiaomi (Mi Home)"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.cloud = XiaomiCloud(hub.data_dir / "xiaomi_session.json")
        self.poll = max(15, int(self.cfg.get("poll_seconds", 60)))
        self.spec_dir = hub.data_dir / "specs"
        self.spec_dir.mkdir(exist_ok=True)
        self._wake = asyncio.Event()
        self._devs = {}       # did -> {country, items, raw}
        self._codes = {}      # (did, siid, piid) -> код последнего ответа облака
        self._model_urns = None

    # ---- вход (вызывается из панели) --------------------------------
    async def login_start(self):
        info = await self.cloud.qr_start()
        asyncio.create_task(self._login_wait())
        return info

    async def _login_wait(self):
        try:
            if await self.cloud.qr_wait():
                self.set_status("starting", "Вход выполнен, загружаю устройства")
                self._wake.set()
        except Exception as e:
            self.set_status("need_login", f"Вход не удался: {e}")

    def logout(self):
        self.cloud.logout()
        self.hub.remove_driver_devices(self.name)
        self._devs = {}
        self._wake.set()

    # ---- спецификации -----------------------------------------------
    async def _fetch_json(self, url):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                return json.loads(await r.text())

    async def _spec(self, model, urn):
        f = self.spec_dir / (model.replace("/", "_") + ".json")
        if f.exists():
            try:
                return json.loads(f.read_text("utf-8"))
            except Exception:
                pass
        if not urn:
            if self._model_urns is None:
                self._model_urns = {}
                try:
                    data = await self._fetch_json(SPEC_URL + "instances?status=released")
                    for i in data.get("instances", []):
                        cur = self._model_urns.get(i["model"])
                        if not cur or i.get("version", 0) >= cur[0]:
                            self._model_urns[i["model"]] = (i.get("version", 0), i["type"])
                except Exception:
                    self._model_urns = None
            urn = (self._model_urns or {}).get(model, (0, None))[1]
        if not urn:
            return None
        try:
            spec = await self._fetch_json(SPEC_URL + "instance?type=" + urn)
        except Exception:
            return None
        f.write_text(json.dumps(spec), "utf-8")
        return spec

    # ---- основной цикл ----------------------------------------------
    async def run(self):
        last_list = 0.0
        while True:
            if not self.cloud.logged_in:
                self.set_status("need_login", "Войдите в аккаунт Mi Home")
                self._wake.clear()
                await self._wake.wait()
                last_list = 0.0
                continue
            try:
                if time.time() - last_list > 600 or not self._devs:
                    await self._load_devices()
                    last_list = time.time()
                await self._poll()
                n = len(self._devs)
                self.set_status("ok", f"Устройств: {n}" if n else "В аккаунте Mi Home нет устройств")
            except AuthError as e:
                self.cloud.s.pop("serviceToken", None)
                self.set_status("need_login", str(e))
                continue
            except Exception as e:
                self.set_status("error", f"{type(e).__name__}: {e}"[:200])
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll)
            except asyncio.TimeoutError:
                pass

    async def _countries(self):
        c = self.cfg.get("country", "auto")
        if c and c != "auto":
            return [c]
        if self.cloud.s.get("countries"):
            return self.cloud.s["countries"]
        found = []
        for country in COUNTRIES:
            try:
                if await self.cloud.devices(country):
                    found.append(country)
            except AuthError:
                raise
            except Exception:
                continue
        if found:
            self.cloud.s["countries"] = found
            self.cloud._save()
        return found

    async def _load_devices(self):
        devs = {}
        for country in await self._countries():
            raw = await self.cloud.devices(country)
            rooms = await self.cloud.rooms(country)
            for d in raw:
                did = str(d.get("did"))
                model = d.get("model") or ""
                spec = await self._spec(model, d.get("spec_type")) if model else None
                devs[did] = {"country": country, "raw": d,
                             "items": map_spec(spec) if spec else [],
                             "category": urn_name(spec.get("type")) if spec else "",
                             "spec": spec_summary(spec) if spec else [],
                             "room": rooms.get(did, "")}
        for did in set(self._devs) - set(devs):
            self.hub.devices.pop(f"xiaomi:{did}", None)
        self._devs = devs
        for did, x in devs.items():
            d = x["raw"]
            self.hub.device(f"xiaomi:{did}", name=d.get("name") or d.get("model") or did,
                            driver=self.name, model=d.get("model") or "", room=x["room"],
                            category=x["category"],
                            online=bool(d.get("isOnline", True)))
        self.hub.notify()

    async def _poll(self):
        by_country = {}
        for did, x in self._devs.items():
            for it in x["items"]:
                by_country.setdefault(x["country"], []).append(
                    {"did": did, "siid": it["siid"], "piid": it["piid"]})
        for country, items in by_country.items():
            for r in await self.cloud.get_props(country, items):
                self._apply(r)

    def _apply(self, r):
        did = str(r.get("did"))
        x = self._devs.get(did)
        self._codes[(did, r.get("siid"), r.get("piid"))] = r.get("code")
        if not x or r.get("code") != 0 or "value" not in r:
            return
        for it in x["items"]:
            if it["siid"] == r.get("siid") and it["piid"] == r.get("piid"):
                ts = r.get("updateTime") or None
                self.hub.update(f"xiaomi:{did}", it["eid"], it["key"], convert(it, r["value"]),
                                name=it.get("name"), unit=it.get("unit"),
                                writable=it["writable"], ts=ts)

    async def set(self, device, entity, value):
        did = device.id.split(":", 1)[1]
        x = self._devs.get(did)
        eid = entity.id.rsplit("/", 1)[1]
        it = next((i for i in (x or {}).get("items", []) if i["eid"] == eid), None)
        if not it or not it["writable"]:
            raise RuntimeError("Это устройство не управляется")
        await self.cloud.set_prop(x["country"], did, it["siid"], it["piid"], bool(value))
        self.hub.update(device.id, eid, it["key"], bool(value), writable=True)

    def diagnostics(self):
        """Список устройств без секретов — для разбора, если что-то не отображается."""
        out = []
        for did, x in self._devs.items():
            d = x["raw"]
            out.append({"did": did, "name": d.get("name"), "model": d.get("model"),
                        "spec_type": d.get("spec_type"), "online": d.get("isOnline"),
                        "parent": d.get("parent_id"), "country": x["country"],
                        "category": x.get("category"),
                        "mapped": [f"{i['eid']} {i['siid']}.{i['piid']} код={self._codes.get((did, i['siid'], i['piid']))}"
                                   for i in x["items"]],
                        "spec": x.get("spec"),
                        "list_prop": d.get("prop"), "list_event": d.get("event")})
        return out
