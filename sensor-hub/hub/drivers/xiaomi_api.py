"""Клиент облака Xiaomi (Mi Home): вход по QR-коду и подписанные запросы.

Протокол сверен с открытым проектом PiotrMachowski/Xiaomi-cloud-tokens-extractor (MIT).
Пароль от аккаунта хабу не нужен: вход подтверждается в приложении Mi Home.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time
from pathlib import Path

import aiohttp

COUNTRIES = ["ru", "de", "sg", "us", "cn", "i2"]


class AuthError(Exception):
    pass


def rc4(key: bytes, data: bytes, drop: int = 1024) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 255
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for n in range(drop + len(data)):
        i = (i + 1) & 255
        j = (j + s[i]) & 255
        s[i], s[j] = s[j], s[i]
        if n >= drop:
            out.append(data[n - drop] ^ s[(s[i] + s[j]) & 255])
    return bytes(out)


def gen_nonce(millis=None) -> str:
    millis = millis or int(time.time() * 1000)
    return base64.b64encode(os.urandom(8) + int(millis / 60000).to_bytes(4, "big")).decode()


def signed_nonce(ssecurity: str, nonce: str) -> str:
    h = hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce))
    return base64.b64encode(h.digest()).decode()


def enc_signature(path: str, method: str, snonce: str, params: dict) -> str:
    parts = [method.upper(), path] + [f"{k}={v}" for k, v in params.items()] + [snonce]
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()


def enc_params(path: str, snonce: str, nonce: str, params: dict, ssecurity: str) -> dict:
    """Зашифровать параметры запроса (RC4) и добавить подпись."""
    key = base64.b64decode(snonce)
    p = dict(params)
    p["rc4_hash__"] = enc_signature(path, "POST", snonce, p)
    p = {k: base64.b64encode(rc4(key, v.encode())).decode() for k, v in p.items()}
    p.update({"signature": enc_signature(path, "POST", snonce, p),
              "ssecurity": ssecurity, "_nonce": nonce})
    return p


def decrypt_response(snonce: str, text: str) -> bytes:
    return rc4(base64.b64decode(snonce), base64.b64decode(text))


def _json(text: str):
    return json.loads(text.replace("&&&START&&&", ""))


def _agent():
    a = "".join(chr(random.randint(65, 69)) for _ in range(13))
    r = "".join(chr(random.randint(97, 122)) for _ in range(18))
    return f"{r}-{a} APP/com.xiaomi.mihome APPV/10.5.201"


class XiaomiCloud:
    ACCOUNT = "https://account.xiaomi.com"

    def __init__(self, session_file: Path):
        self.file = Path(session_file)
        self.s = {}
        try:
            self.s = json.loads(self.file.read_text("utf-8"))
        except Exception:
            pass
        self.s.setdefault("agent", _agent())
        self._qr = None
        self._http: aiohttp.ClientSession | None = None

    # ---- сессия -----------------------------------------------------
    @property
    def logged_in(self):
        return bool(self.s.get("serviceToken") and self.s.get("ssecurity"))

    def _save(self):
        self.file.write_text(json.dumps(self.s), "utf-8")

    def logout(self):
        self.s = {"agent": self.s["agent"]}
        self._save()

    def api_url(self, country):
        return "https://" + ("" if country == "cn" else country + ".") + "api.io.mi.com/app"

    async def _client(self):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(),
                                               timeout=aiohttp.ClientTimeout(total=30))
        return self._http

    async def close(self):
        if self._http and not self._http.closed:
            await self._http.close()

    # ---- вход по QR -------------------------------------------------
    async def qr_start(self) -> dict:
        """Начать вход. Возвращает {'qr': картинка, 'login_url': ссылка}."""
        jar = aiohttp.CookieJar(unsafe=True)
        sess = aiohttp.ClientSession(cookie_jar=jar, headers={"User-Agent": self.s["agent"]})
        params = {"_qrsize": "480", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
                  "callback": "https://sts.api.io.mi.com/sts", "_hasLogo": "false",
                  "sid": "xiaomiio", "serviceParam": "", "_locale": "en_GB",
                  "_dc": str(int(time.time() * 1000))}
        try:
            async with sess.get(self.ACCOUNT + "/longPolling/loginUrl", params=params,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = _json(await r.text())
        except Exception:
            await sess.close()
            raise
        if "qr" not in data:
            await sess.close()
            raise RuntimeError("Xiaomi не выдал код для входа")
        if self._qr:
            await self._qr["sess"].close()
        self._qr = {"sess": sess, "jar": jar, "lp": data["lp"],
                    "deadline": time.time() + int(data.get("timeout", 300))}
        return {"qr": data["qr"], "login_url": data["loginUrl"]}

    async def qr_wait(self) -> bool:
        """Ждать подтверждения входа в Mi Home. True — вошли."""
        q = self._qr
        if not q:
            return False
        sess = q["sess"]
        try:
            data = None
            while time.time() < q["deadline"]:
                try:
                    async with sess.get(q["lp"], timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            data = _json(await r.text())
                            break
                except (TimeoutError, aiohttp.ClientError):
                    continue
            if not data or "ssecurity" not in data:
                return False
            token = await self._service_token(sess, q["jar"], data["location"])
            if not token:
                return False
            self.s.update({"userId": str(data["userId"]), "cUserId": data.get("cUserId"),
                           "ssecurity": data["ssecurity"], "passToken": data.get("passToken"),
                           "serviceToken": token})
            self.s.pop("countries", None)
            self._save()
            return True
        finally:
            await sess.close()
            self._qr = None

    @staticmethod
    async def _service_token(sess, jar, location):
        async with sess.get(location) as r:
            await r.read()
            for resp in list(r.history) + [r]:
                if "serviceToken" in resp.cookies:
                    return resp.cookies["serviceToken"].value
        for c in jar:
            if c.key == "serviceToken":
                return c.value
        return None

    async def refresh(self) -> bool:
        """Обновить истёкший serviceToken по сохранённому passToken (без нового входа)."""
        if not self.s.get("passToken"):
            return False
        jar = aiohttp.CookieJar(unsafe=True)
        cookie = f"userId={self.s['userId']}; passToken={self.s['passToken']}"
        try:
            async with aiohttp.ClientSession(cookie_jar=jar, timeout=aiohttp.ClientTimeout(total=20),
                                             headers={"User-Agent": self.s["agent"]}) as sess:
                async with sess.get(self.ACCOUNT + "/pass/serviceLogin?sid=xiaomiio&_json=true",
                                    headers={"Cookie": cookie}) as r:
                    data = _json(await r.text())
                if data.get("code") != 0 or not data.get("location") or not data.get("ssecurity"):
                    return False
                token = await self._service_token(sess, jar, data["location"])
                if not token:
                    return False
                self.s.update({"ssecurity": data["ssecurity"], "serviceToken": token})
                self._save()
                return True
        except Exception:
            return False

    # ---- запросы ----------------------------------------------------
    async def call(self, country: str, path: str, data: dict, _retry=True):
        if not self.logged_in:
            raise AuthError("Нужен вход в Xiaomi")
        nonce = gen_nonce()
        sn = signed_nonce(self.s["ssecurity"], nonce)
        fields = enc_params(path, sn, nonce, {"data": json.dumps(data, separators=(",", ":"))},
                            self.s["ssecurity"])
        headers = {
            "Accept-Encoding": "identity", "User-Agent": self.s["agent"],
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
            "Cookie": "; ".join(f"{k}={v}" for k, v in {
                "userId": self.s["userId"], "yetAnotherServiceToken": self.s["serviceToken"],
                "serviceToken": self.s["serviceToken"], "locale": "ru_RU",
                "timezone": "GMT+10:00", "is_daylight": "0", "dst_offset": "0",
                "channel": "MI_APP_STORE"}.items()),
        }
        http = await self._client()
        async with http.post(self.api_url(country) + path, params=fields, headers=headers) as r:
            status, text = r.status, await r.text()
        if status in (401, 403):
            if _retry and await self.refresh():
                return await self.call(country, path, data, _retry=False)
            raise AuthError("Сессия Xiaomi истекла, войдите заново")
        if status != 200:
            raise RuntimeError(f"Облако Xiaomi ответило {status}")
        try:
            body = json.loads(decrypt_response(sn, text))
        except Exception:
            raise RuntimeError("Не удалось прочитать ответ облака Xiaomi")
        if body.get("code") not in (0, None) and "result" not in body:
            raise RuntimeError(f"Xiaomi: {body.get('message') or body.get('code')}")
        return body.get("result")

    async def devices(self, country):
        res = await self.call(country, "/home/device_list", {
            "getVirtualModel": True, "getHuamiDevices": 1,
            "get_split_device": False, "support_smart_home": True})
        return (res or {}).get("list") or []

    async def rooms(self, country) -> dict:
        """did -> название комнаты (если удалось получить)."""
        out = {}
        try:
            res = await self.call(country, "/v2/homeroom/gethome", {
                "fg": True, "fetch_share": True, "fetch_share_dev": True, "limit": 300, "app_ver": 7})
            for home in (res or {}).get("homelist") or []:
                for room in home.get("roomlist") or []:
                    for did in room.get("dids") or []:
                        out[str(did)] = room.get("name", "")
        except AuthError:
            raise
        except Exception:
            pass
        return out

    async def get_props(self, country, items):
        """items: [{did, siid, piid}] -> список результатов с value."""
        out = []
        for i in range(0, len(items), 40):
            res = await self.call(country, "/miotspec/prop/get",
                                  {"datasource": 1, "params": items[i:i + 40]})
            out += res or []
        return out

    async def last_records(self, country, did, key, type_="event", days=30, limit=3):
        """Последние записи журнала устройства (события или свойства), новые первыми."""
        now = int(time.time())
        res = await self.call(country, "/user/get_user_device_data", {
            "did": did, "key": key, "type": type_,
            "time_start": now - days * 86400, "time_end": now + 60, "limit": limit})
        rows = res if isinstance(res, list) else []
        return sorted(rows, key=lambda r: r.get("time", 0), reverse=True)

    async def action(self, country, did, siid, aiid, args):
        res = await self.call(country, "/miotspec/action",
                              {"params": {"did": did, "siid": siid, "aiid": aiid, "in": args}})
        return res if isinstance(res, dict) else {}

    async def set_prop(self, country, did, siid, piid, value):
        res = await self.call(country, "/miotspec/prop/set",
                              {"params": [{"did": did, "siid": siid, "piid": piid, "value": value}]})
        code = (res or [{}])[0].get("code", -1)
        if code not in (0, 1):
            raise RuntimeError(f"Устройство не приняло команду (код {code})")
