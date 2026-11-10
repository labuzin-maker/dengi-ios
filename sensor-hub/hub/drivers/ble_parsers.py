"""Разбор рекламных пакетов Bluetooth-датчиков (без подключения к ним).

Поддержаны форматы: BTHome v2 (Shelly BLU и др.), ATC/pvvx (перепрошитые
термометры Xiaomi), MiBeacon без шифрования (старые датчики Xiaomi).
Каждая функция возвращает словарь {ключ: значение} или None.
"""
from __future__ import annotations

import struct

UUID_BTHOME = "0000fcd2-0000-1000-8000-00805f9b34fb"
UUID_ATC = "0000181a-0000-1000-8000-00805f9b34fb"
UUID_MI = "0000fe95-0000-1000-8000-00805f9b34fb"

# id объекта BTHome -> (длина, знаковое, множитель, наш ключ или None)
_BTHOME = {
    0x00: (1, False, 1, None), 0x01: (1, False, 1, "battery"),
    0x02: (2, True, 0.01, "temperature"), 0x03: (2, False, 0.01, "humidity"),
    0x04: (3, False, 0.01, "pressure"), 0x05: (3, False, 0.01, "illuminance"),
    0x06: (2, False, 0.01, None), 0x07: (2, False, 0.01, None), 0x08: (2, True, 0.01, None),
    0x09: (1, False, 1, None), 0x0A: (3, False, 0.001, "energy"),
    0x0B: (3, False, 0.01, "power"), 0x0C: (2, False, 0.001, "voltage"),
    0x0D: (2, False, 1, "pm25"), 0x0E: (2, False, 1, "pm10"),
    0x12: (2, False, 1, "co2"), 0x13: (2, False, 1, "tvoc"),
    0x14: (2, False, 0.01, "moisture"), 0x2E: (1, False, 1, "humidity"),
    0x2F: (1, False, 1, "moisture"), 0x3A: (1, False, 1, None), 0x3C: (2, False, 1, None),
    0x3D: (2, False, 1, None), 0x3E: (4, False, 1, None), 0x3F: (2, True, 0.1, None),
    0x40: (2, False, 1, None), 0x41: (2, False, 0.1, None), 0x42: (3, False, 0.001, None),
    0x43: (2, False, 0.001, None), 0x44: (2, False, 0.01, None),
    0x45: (2, True, 0.1, "temperature"), 0x46: (1, False, 0.1, None),
    0x47: (2, False, 0.1, None), 0x48: (2, False, 1, None), 0x49: (2, False, 1, None),
    0x4A: (2, False, 0.1, "voltage"), 0x4B: (3, False, 0.001, None),
    0x4C: (4, False, 0.001, None), 0x4D: (4, False, 0.001, "energy"),
    0x4E: (4, False, 0.001, None), 0x4F: (4, False, 0.001, None), 0x50: (4, False, 1, None),
    0x51: (2, False, 0.001, None), 0x52: (2, False, 0.001, None),
    0x57: (1, True, 1, "temperature"), 0x58: (1, True, 0.35, "temperature"),
    0xF0: (2, False, 1, None), 0xF1: (4, False, 1, None), 0xF2: (3, False, 1, None),
}
# двоичные датчики BTHome (все по 1 байту)
_BTHOME_BIN = {0x10: "switch_state", 0x11: "contact", 0x1A: "contact", 0x1B: "contact",
               0x20: "leak", 0x21: "motion", 0x23: "motion", 0x25: "motion",
               0x29: "smoke", 0x2D: "contact"}
for _i in range(0x0F, 0x2E):
    _BTHOME.setdefault(_i, (1, False, 1, None))


def parse_bthome(data: bytes):
    if len(data) < 2:
        return None
    info = data[0]
    if (info >> 5) != 2:
        return None
    if info & 1:
        return {"_encrypted": True}
    out, i = {}, 1
    while i < len(data):
        oid = data[i]
        spec = _BTHOME.get(oid)
        if spec is None:
            break
        size, signed, factor, key = spec
        raw = data[i + 1:i + 1 + size]
        if len(raw) < size:
            break
        val = int.from_bytes(raw, "little", signed=signed)
        if oid in _BTHOME_BIN:
            k = _BTHOME_BIN[oid]
            if k != "switch_state":
                out.setdefault(k, bool(val))
        elif key:
            out.setdefault(key, round(val * factor, 2) if factor != 1 else val)
        i += 1 + size
    return out or None


def parse_atc(data: bytes):
    if len(data) == 13:      # прошивка atc1441
        temp, hum, batt, mv = struct.unpack(">hBBH", data[6:12])
        return {"temperature": temp / 10, "humidity": hum, "battery": batt, "voltage": mv / 1000}
    if len(data) == 15:      # прошивка pvvx
        temp, hum, mv, batt = struct.unpack("<hHHB", data[6:13])
        return {"temperature": temp / 100, "humidity": hum / 100, "battery": batt,
                "voltage": mv / 1000}
    return None


def parse_mibeacon(data: bytes):
    if len(data) < 5:
        return None
    fc = data[0] | (data[1] << 8)
    if fc & 0x08:
        return {"_encrypted": True}
    i = 5
    if fc & 0x10:
        i += 6               # MAC
    if fc & 0x20:
        if i >= len(data):
            return None
        cap = data[i]
        i += 1
        if cap & 0x20:
            i += 2
    if not fc & 0x40:
        return None
    out = {}
    while i + 3 <= len(data):
        oid = data[i] | (data[i + 1] << 8)
        size = data[i + 2]
        p = data[i + 3:i + 3 + size]
        if len(p) < size:
            break
        if oid == 0x1004 and size == 2:
            out["temperature"] = struct.unpack("<h", p)[0] / 10
        elif oid == 0x1006 and size == 2:
            out["humidity"] = struct.unpack("<H", p)[0] / 10
        elif oid == 0x100D and size == 4:
            t, h = struct.unpack("<hH", p)
            out["temperature"], out["humidity"] = t / 10, h / 10
        elif oid == 0x100A and size >= 1:
            out["battery"] = p[0]
        elif oid == 0x1007 and size == 3:
            out["illuminance"] = int.from_bytes(p, "little")
        elif oid == 0x1008 and size >= 1:
            out["moisture"] = p[0]
        elif oid == 0x1009 and size == 2:
            out["conductivity"] = struct.unpack("<H", p)[0]
        elif oid == 0x1014 and size >= 1:
            out["leak"] = bool(p[0])
        elif oid == 0x1019 and size >= 1:
            out["contact"] = p[0] == 0      # 0 — открыто
        elif oid == 0x000F and size == 3:
            out["motion"] = True
            out["illuminance"] = int.from_bytes(p, "little")
        i += 3 + size
    return out or None


def parse_advert(service_data: dict):
    """service_data: {uuid: bytes}. Возвращает (формат, показания) или (None, None)."""
    for uuid, fn, label in ((UUID_BTHOME, parse_bthome, "BTHome"),
                            (UUID_ATC, parse_atc, "ATC"),
                            (UUID_MI, parse_mibeacon, "Xiaomi")):
        raw = service_data.get(uuid)
        if raw:
            try:
                res = fn(bytes(raw))
            except Exception:
                res = None
            if res:
                return label, res
    return None, None
