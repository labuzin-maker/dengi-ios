"""Ядро хаба: модель устройств, общее состояние, история, подписчики."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

# Известные типы показаний: ключ -> (название, единица, вид)
# вид: sensor (число), binary (да/нет), switch (управляемый выключатель)
KEYS = {
    "temperature": ("Температура", "°C", "sensor"),
    "humidity": ("Влажность", "%", "sensor"),
    "battery": ("Батарея", "%", "sensor"),
    "illuminance": ("Освещённость", "лк", "sensor"),
    "pressure": ("Давление", "гПа", "sensor"),
    "pm25": ("PM2.5", "мкг/м³", "sensor"),
    "pm10": ("PM10", "мкг/м³", "sensor"),
    "co2": ("CO₂", "ppm", "sensor"),
    "tvoc": ("ЛОС", "мкг/м³", "sensor"),
    "moisture": ("Влажность почвы", "%", "sensor"),
    "conductivity": ("Удобрение", "мкСм/см", "sensor"),
    "power": ("Мощность", "Вт", "sensor"),
    "energy": ("Энергия", "кВт·ч", "sensor"),
    "voltage": ("Напряжение", "В", "sensor"),
    "device_temp": ("Нагрев корпуса", "°C", "sensor"),
    "motion": ("Движение", "", "binary"),
    "contact": ("Открыто", "", "binary"),
    "leak": ("Протечка", "", "binary"),
    "smoke": ("Дым", "", "binary"),
    "switch": ("Питание", "", "switch"),
    # настройки: number — число в пределах min..max, select — выбор из списка
    "target_humidity": ("Целевая влажность", "%", "number"),
    "target_temperature": ("Целевая температура", "°C", "number"),
    "brightness": ("Яркость", "%", "number"),
    "fan_level": ("Режим", "", "select"),
    "mode": ("Режим работы", "", "select"),
    "night_shot": ("Ночной режим", "", "select"),
    "recording_mode": ("Запись на карту", "", "select"),
    "image_rollover": ("Переворот изображения", "°", "number"),
    # toggle — настройка «да/нет», показывается только в карточке устройства
    "time_watermark": ("Метка времени на видео", "", "toggle"),
    "wdr_mode": ("Расширенный диапазон (WDR)", "", "toggle"),
    "full_color": ("Цвет при слабом свете", "", "toggle"),
}


@dataclass
class Entity:
    id: str
    key: str
    kind: str
    name: str
    unit: str = ""
    value: object = None
    ts: float = 0.0
    writable: bool = False
    opts: dict = None       # для number: min/max/step, для select: options

    def as_dict(self):
        return {"id": self.id, "key": self.key, "kind": self.kind, "name": self.name,
                "unit": self.unit, "value": self.value, "ts": self.ts, "writable": self.writable,
                "opts": self.opts}


@dataclass
class Device:
    id: str
    name: str
    driver: str
    model: str = ""
    room: str = ""
    online: bool = True
    category: str = ""
    caps: tuple = ()        # дополнительные возможности, например "video"
    extra: dict = None      # сведения для панели, например варианты качества видео
    entities: dict = field(default_factory=dict)


class Hub:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.devices: dict[str, Device] = {}
        self.drivers: dict[str, object] = {}
        self._listeners: set[asyncio.Queue] = set()
        self._ov_file = self.data_dir / "overrides.json"
        try:
            self.overrides = json.loads(self._ov_file.read_text("utf-8"))
        except Exception:
            self.overrides = {}
        self._db = sqlite3.connect(self.data_dir / "history.db")
        self._db.execute("CREATE TABLE IF NOT EXISTS samples (entity TEXT, ts REAL, value REAL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix ON samples (entity, ts)")
        self._last_saved: dict[str, tuple] = {}
        self._last_prune = 0.0

    # ---- устройства -------------------------------------------------
    def device(self, dev_id, *, name, driver, model="", room="", online=True, category="", caps=(), extra=None) -> Device:
        d = self.devices.get(dev_id)
        if d is None:
            d = self.devices[dev_id] = Device(dev_id, name, driver, model, room, online, category, tuple(caps), extra)
            self.notify()
        else:
            changed = (d.name, d.model, d.room, d.online) != (name, model or d.model, room or d.room, online)
            d.name, d.model, d.room, d.online = name, model or d.model, room or d.room, online
            d.category = category or d.category
            d.caps = tuple(caps) or d.caps
            d.extra = extra or d.extra
            if changed:
                self.notify()
        return d

    def update(self, dev_id, eid, key, value, *, name=None, unit=None, kind=None,
               writable=False, ts=None, opts=None):
        """Записать показание. eid — уникальное имя внутри устройства, key — тип из KEYS."""
        d = self.devices.get(dev_id)
        if d is None:
            return
        label, def_unit, def_kind = KEYS.get(key, (key, "", "sensor"))
        full = f"{dev_id}/{eid}"
        e = d.entities.get(eid)
        if e is None:
            e = d.entities[eid] = Entity(full, key, kind or def_kind, name or label,
                                         def_unit if unit is None else unit, writable=writable, opts=opts)
        if e.kind in ("binary", "switch", "toggle") and value is not None:
            value = bool(value)
        elif isinstance(value, float):
            value = round(value, 2)
        ts = ts or time.time()
        if value != e.value:
            e.value, e.ts = value, ts
            self.notify()
        else:
            e.ts = max(e.ts, ts)
        self._save_sample(e)

    def remove_driver_devices(self, driver):
        ids = [i for i, d in self.devices.items() if d.driver == driver]
        for i in ids:
            del self.devices[i]
        if ids:
            self.notify()

    def has_real_devices(self):
        return any(d.driver != "demo" for d in self.devices.values())

    def find_entity(self, entity_id):
        dev_id, _, eid = entity_id.rpartition("/")
        d = self.devices.get(dev_id)
        return (d, d.entities.get(eid)) if d else (None, None)

    # ---- пользовательские имена и комнаты ---------------------------
    def set_override(self, dev_id, **kw):
        o = self.overrides.setdefault(dev_id, {})
        for k, v in kw.items():
            if v is None:
                continue
            if v == "" and k != "hidden":
                o.pop(k, None)
            else:
                o[k] = v
        if not o:
            self.overrides.pop(dev_id, None)
        self._ov_file.write_text(json.dumps(self.overrides, ensure_ascii=False, indent=1), "utf-8")
        self.notify()

    # ---- снимок состояния -------------------------------------------
    def snapshot(self):
        devs = []
        now = time.time()
        for d in self.devices.values():
            o = self.overrides.get(d.id, {})
            # датчики на батарейке облако часто считает «не в сети», хотя данные свежие
            fresh = any(e.ts > now - 7200 for e in d.entities.values())
            devs.append({
                "id": d.id, "name": o.get("name") or d.name, "orig_name": d.name,
                "room": o.get("room") or d.room or "Без комнаты", "driver": d.driver,
                "model": d.model, "category": d.category, "caps": list(d.caps), "extra": d.extra, "online": d.online or fresh, "hidden": bool(o.get("hidden")),
                "entities": [e.as_dict() for e in d.entities.values()],
            })
        devs.sort(key=lambda x: (x["room"] == "Без комнаты", x["room"], x["name"]))
        drv = [{"name": n, "title": x.title, "status": x.status, "message": x.message}
               for n, x in self.drivers.items()]
        return {"time": time.time(), "devices": devs, "drivers": drv}

    # ---- подписчики (живое обновление панели) -----------------------
    def subscribe(self):
        q = asyncio.Queue(maxsize=1)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q):
        self._listeners.discard(q)

    def notify(self):
        for q in self._listeners:
            if q.empty():
                q.put_nowait(1)

    # ---- история ----------------------------------------------------
    def _save_sample(self, e: Entity):
        if e.value is None or isinstance(e.value, str) or e.kind in ("number", "select"):
            return
        v = float(e.value)
        last = self._last_saved.get(e.id)
        now = time.time()
        # пишем при изменении или раз в 10 минут
        if last and last[1] == v and now - last[0] < 600:
            return
        self._last_saved[e.id] = (now, v)
        self._db.execute("INSERT INTO samples VALUES (?,?,?)", (e.id, now, v))
        if now - self._last_prune > 3600:
            self._last_prune = now
            self._db.execute("DELETE FROM samples WHERE ts < ?", (now - 30 * 86400,))
        self._db.commit()

    def history(self, entity_id, hours=24.0):
        since = time.time() - hours * 3600
        rows = self._db.execute(
            "SELECT ts, value FROM samples WHERE entity=? AND ts>=? ORDER BY ts", (entity_id, since)
        ).fetchall()
        return [[round(t), v] for t, v in rows]


    def sparks(self, hours=24.0, buckets=36, keys=("temperature", "humidity")):
        """Короткие ряды для мини-графиков: {entity_id: [значения по равным отрезкам времени]}."""
        now = time.time()
        since = now - hours * 3600
        step = hours * 3600 / buckets
        out = {}
        for d in self.devices.values():
            for e in d.entities.values():
                if e.key not in keys:
                    continue
                rows = self._db.execute("SELECT ts, value FROM samples WHERE entity=? AND ts>=? ORDER BY ts",
                                        (e.id, since)).fetchall()
                if len(rows) < 2:
                    continue
                sums = [[0.0, 0] for _ in range(buckets)]
                for t, v in rows:
                    b = min(buckets - 1, int((t - since) / step))
                    sums[b][0] += v
                    sums[b][1] += 1
                vals, last = [], None
                for s, n in sums:
                    last = round(s / n, 2) if n else last
                    vals.append(last)
                out[e.id] = [v for v in vals if v is not None]
        return out


class Driver:
    """Базовый класс источника датчиков."""
    name = ""
    title = ""

    def __init__(self, hub: Hub, cfg: dict):
        self.hub = hub
        self.cfg = cfg or {}
        self.status = "starting"   # ok | error | need_login | starting
        self.message = ""

    def set_status(self, status, message=""):
        if (status, message) != (self.status, self.message):
            self.status, self.message = status, message
            self.hub.notify()

    async def run(self):
        raise NotImplementedError

    async def set(self, device: Device, entity: Entity, value):
        raise RuntimeError("Это устройство не управляется")

    async def video(self, device: Device, quality=None) -> dict:
        raise RuntimeError("У этого устройства нет видео")
