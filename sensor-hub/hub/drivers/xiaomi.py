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


# настройки, которыми можно управлять: имя свойства -> наш ключ
CONTROL_MAP = {"target-humidity": "target_humidity", "target-temperature": "target_temperature",
               "fan-level": "fan_level", "brightness": "brightness", "mode": "mode",
               "night-shot": "night_shot", "recording-mode": "recording_mode",
               "image-rollover": "image_rollover", "time-watermark": "time_watermark",
               "wdr-mode": "wdr_mode", "glimmer-full-color": "full_color"}


def option_label(text) -> str:
    """Перевести название варианта из каталога Xiaomi."""
    t = str(text or "").strip()
    low = t.lower().replace("_", " ")
    for word, ru in (("level", "Скорость"), ("gear", "Скорость")):
        if low.startswith(word):
            num = low[len(word):].strip()
            if num.isdigit():
                return f"{ru} {num}"
    return {"auto": "Авто", "humidity": "Авто (по влажности)", "const humidity": "Авто (по влажности)",
            "sleep": "Ночной", "silent": "Тихий", "low": "Слабо", "medium": "Средне", "mid": "Средне",
            "high": "Сильно", "strong": "Сильно", "favorite": "Свой", "standard": "Обычный",
            "natural wind": "Естественный", "straight wind": "Прямой", "idle": "Ожидание",
            "on": "Вкл", "off": "Выкл", "open": "Вкл", "close": "Выкл",
            "not recording": "Не записывать", "all record": "Постоянно",
            "motion record": "По движению"}.get(low, t)


def quality_label(text) -> str:
    """'1920_1080_20' -> '1080p', 'Auto' -> 'Авто'."""
    t = str(text or "")
    parts = t.split("_")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[1]}p"
    return "Авто" if t.lower() == "auto" else t


def find_stream(spec: dict):
    """Найти в описании камеры команду запуска трансляции HLS (служба для Google Home)."""
    for srv in spec.get("services") or []:
        props = {p["iid"]: p for p in srv.get("properties") or []}
        for act in srv.get("actions") or []:
            if urn_name(act.get("type")) != "start-hls-stream":
                continue
            args, quality, qpos = [], [], None
            for n, piid in enumerate(act.get("in") or []):
                prop = props.get(piid) or {}
                lst = prop.get("value-list") or []
                rng = prop.get("value-range") or [0]
                args.append(lst[0]["value"] if lst else rng[0])
                if urn_name(prop.get("type")) == "video-attribute" and len(lst) > 1:
                    qpos = n
                    quality = [{"value": o["value"], "label": quality_label(o.get("description"))} for o in lst]
            outs = [urn_name((props.get(p) or {}).get("type")) for p in act.get("out") or []]
            return {"siid": srv["iid"], "aiid": act["iid"], "in": args, "out": outs,
                    "quality": quality, "qpos": qpos}
    return None


def map_controls(spec: dict) -> list[dict]:
    """Настройки устройства: целевая влажность, скорость и т.п."""
    out, seen = [], set()
    for srv in spec.get("services") or []:
        sname = urn_name(srv.get("type"))
        if sname in SKIP_ON_SERVICES or sname == "device-information":
            continue
        for prop in srv.get("properties") or []:
            key = CONTROL_MAP.get(urn_name(prop.get("type")))
            access = prop.get("access") or []
            if not key or key in seen or "write" not in access or "read" not in access:
                continue
            item = {"key": key, "eid": key, "siid": srv["iid"], "piid": prop["iid"], "writable": True,
                    "name": None, "float": prop.get("format") == "float"}
            rng, lst = prop.get("value-range"), prop.get("value-list")
            if prop.get("format") == "bool":
                item["kind"] = "toggle"
            elif lst:
                item["kind"] = "select"
                item["opts"] = {"options": [{"value": o.get("value"), "label": option_label(o.get("description"))}
                                            for o in lst]}
            elif rng and len(rng) >= 2:
                item["kind"] = "number"
                item["opts"] = {"min": rng[0], "max": rng[1], "step": rng[2] if len(rng) > 2 and rng[2] else 1}
            else:
                continue
            seen.add(key)
            out.append(item)
    return out


# события, из которых складывается состояние датчика: имя -> (наш ключ, значение)
EVENT_MAP = {
    "submersion-detected": ("leak", True), "no-submersion": ("leak", False),
    "open": ("contact", True), "close": ("contact", False),
    "motion-detected": ("motion", True),
}
# то же для старого журнала устройств Aqara (device_log)
LOG_MAP = {"event.leak": ("leak", True), "event.no_leak": ("leak", False),
           "event.open": ("contact", True), "event.close": ("contact", False),
           "event.motion": ("motion", True)}
MOTION_HOLD = 120   # секунд считаем «движение» после события


def map_events(spec: dict, have: set) -> list[dict]:
    """События датчиков, которые сообщают состояние только событиями (протечка, дверь, движение)."""
    out = []
    for srv in spec.get("services") or []:
        for ev in srv.get("events") or []:
            m = EVENT_MAP.get(urn_name(ev.get("type")))
            if m and m[0] not in have:
                out.append({"key": m[0], "value": m[1], "ekey": f"{srv['iid']}.{ev['iid']}"})
    return out


def state_from_events(key, records, now):
    """records: [(время, значение)]. Возвращает текущее состояние."""
    if not records:
        return False            # событий не было: сухо / закрыто / тихо
    t, v = max(records)
    if key == "motion":
        return now - t < MOTION_HOLD
    return v


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
        self.stream_wait = 25      # сколько секунд ждать запуска трансляции камеры
        self.spec_dir = hub.data_dir / "specs"
        self.spec_dir.mkdir(exist_ok=True)
        self._wake = asyncio.Event()
        self._devs = {}       # did -> {country, items, raw}
        self._codes = {}      # (did, siid, piid) -> код последнего ответа облака
        self._evlog = {}      # did -> что ответило облако про события
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
                items = (map_spec(spec) + map_controls(spec)) if spec else []
                devs[did] = {"country": country, "raw": d,
                             "items": items,
                             "events": map_events(spec, {i["key"] for i in items}) if spec else [],
                             "category": urn_name(spec.get("type")) if spec else "",
                             "stream": find_stream(spec) if spec else None,
                             "spec": spec_summary(spec) if spec else [],
                             "room": rooms.get(did, "")}
        for did in set(self._devs) - set(devs):
            self.hub.devices.pop(f"xiaomi:{did}", None)
        self._devs = devs
        for did, x in devs.items():
            d = x["raw"]
            self.hub.device(f"xiaomi:{did}", name=d.get("name") or d.get("model") or did,
                            driver=self.name, model=d.get("model") or "", room=x["room"],
                            category=x["category"], caps=("video",) if x.get("stream") else (),
                            extra={"quality": x["stream"]["quality"]} if x.get("stream") and x["stream"].get("quality") else None,
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
        for did, x in self._devs.items():
            if x.get("events"):
                try:
                    await self._poll_events(did, x)
                except AuthError:
                    raise
                except Exception as e:
                    self._evlog[did] = f"ошибка: {type(e).__name__}: {e}"[:200]

    async def _poll_events(self, did, x):
        now = time.time()
        by_key, log, seen = {}, [], 0     # seen — сколько записей журнала облако вообще отдало
        for ev in x["events"]:
            rows = await self.cloud.last_records(x["country"], did, ev["ekey"], days=1100)
            seen += len(rows)
            log.append(f"{ev['ekey']}: {len(rows)} зап." + (f", последняя {int(now - rows[0].get('time', 0))} с назад" if rows else ""))
            by_key.setdefault(ev["key"], []).extend((r.get("time", 0), ev["value"]) for r in rows)
        if not any(by_key.values()) and now - x.get("_fb", 0) > 600:
            # запасные способы (раз в 10 минут): старые названия событий и общий журнал device_log
            x["_fb"] = now
            for name, (key, val) in LOG_MAP.items():
                if key not in by_key:
                    continue
                try:
                    rows = await self.cloud.last_records(x["country"], did, name, days=1100)
                except AuthError:
                    raise
                except Exception:
                    continue
                seen += len(rows)
                log.append(f"{name}: {len(rows)} зап.")
                by_key[key].extend((r.get("time", 0), val) for r in rows)
            try:
                rows = await self.cloud.last_records(x["country"], did, "device_log", type_="prop", days=1100, limit=5)
                seen += len(rows)
                log.append(f"device_log: {len(rows)} зап." + (f" {str(rows[0].get('value'))[:80]}" if rows else ""))
                for r in rows:
                    val = json.loads(r.get("value") or "[]")
                    for item in val[1:] if isinstance(val, list) else []:
                        m = LOG_MAP.get(item[0] if isinstance(item, list) and item else None)
                        if m and m[0] in by_key:
                            by_key[m[0]].append((val[0] if isinstance(val[0], (int, float)) else r.get("time", 0), m[1]))
            except AuthError:
                raise
            except Exception as e:
                log.append(f"device_log: ошибка {type(e).__name__}")
        if len(log) > len(x["events"]) or did not in self._evlog:
            self._evlog[did] = "; ".join(log)
        if not seen and not x.get("_seen"):
            # облако не отдало ни одной записи: нельзя утверждать «сухо», оставляем «нет данных»
            return
        x["_seen"] = True
        for key, recs in by_key.items():
            self.hub.update(f"xiaomi:{did}", key, key, state_from_events(key, recs, now), ts=now)

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
                                name=it.get("name"), unit=it.get("unit"), kind=it.get("kind"),
                                writable=it["writable"], ts=ts, opts=it.get("opts"))

    async def set(self, device, entity, value):
        did = device.id.split(":", 1)[1]
        x = self._devs.get(did)
        eid = entity.id.rsplit("/", 1)[1]
        it = next((i for i in (x or {}).get("items", []) if i["eid"] == eid), None)
        if not it or not it["writable"]:
            raise RuntimeError("Это устройство не управляется")
        kind = it.get("kind")
        if kind == "number":
            o = it["opts"]
            value = max(o["min"], min(o["max"], float(value)))
            value = value if it.get("float") else int(round(value))
        elif kind == "select":
            allowed = [o["value"] for o in it["opts"]["options"]]
            value = next((v for v in allowed if str(v) == str(value)), None)
            if value is None:
                raise RuntimeError("Такого варианта у устройства нет")
        else:
            value = bool(value)
        await self.cloud.set_prop(x["country"], did, it["siid"], it["piid"], value)
        self.hub.update(device.id, eid, it["key"], value, writable=True)

    async def video(self, device, quality=None):
        did = device.id.split(":", 1)[1]
        x = self._devs.get(did) or {}
        st = x.get("stream")
        if not st:
            raise RuntimeError("У этой камеры нет службы трансляции")
        args = list(st["in"])
        if quality is not None and st.get("qpos") is not None:
            pick = next((q["value"] for q in st["quality"] if str(q["value"]) == str(quality)), None)
            if pick is not None:
                args[st["qpos"]] = pick
        res = await self.cloud.action(x["country"], did, st["siid"], st["aiid"], args)
        out = res.get("out") or []
        self._evlog[did] = f"видео: качество={args} код={res.get('code')} out={str(out)[:300]}"
        url = None
        if "stream-address" in st["out"] and len(out) > st["out"].index("stream-address"):
            url = out[st["out"].index("stream-address")]
        if not (isinstance(url, str) and url.startswith("http")):
            url = next((o for o in out if isinstance(o, str) and o.startswith("http")), None)
        if not url:
            raise RuntimeError(f"Камера не выдала ссылку на видео (код {res.get('code')})")
        # ждём, пока трансляция действительно начнётся: в плейлисте должны появиться куски видео
        ready, info = await self._wait_stream(url)
        self._evlog[did] += f" | готовность: {ready}, {info}"
        return {"url": url, "ready": ready, "info": info}

    async def _wait_stream(self, url):
        from urllib.parse import urljoin
        deadline = time.time() + self.stream_wait
        info = "не проверялось"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async def text(u):
                async with s.get(u) as r:
                    return r.status, await r.text(errors="replace")
            while True:
                try:
                    status, body = await text(url)
                    if "#EXTINF" in body:
                        return True, "трансляция идёт"
                    variants = [urljoin(url, ln.strip()) for ln in body.splitlines()
                                if ln.strip() and not ln.startswith("#")]
                    if status != 200:
                        info = f"сервер видео ответил {status}"
                    elif not variants:
                        info = "сервер видео вернул пустой список"
                    else:
                        vstatus, vbody = await text(variants[0])
                        if "#EXTINF" in vbody:
                            return True, "трансляция идёт"
                        info = f"трансляция ещё не началась (ответ {vstatus})"
                except Exception as e:
                    info = f"сервер видео недоступен: {type(e).__name__}"
                if time.time() >= deadline:
                    return False, info
                await asyncio.sleep(2)

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
                        "events": self._evlog.get(did), "stream": x.get("stream"),
                        "list_prop": d.get("prop"), "list_event": d.get("event")})
        return out
