"""Проверки: python -m unittest tests.test_all -v"""
import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hub.core import Hub
from hub.drivers import ble_parsers as bp
from hub.drivers import xiaomi_api as xa
from hub.drivers.shelly import ShellyDriver
from hub.drivers.xiaomi import XiaomiDriver, map_spec
from hub.server import make_app

HERE = Path(__file__).parent
SPEC = json.loads((HERE / "spec_lywsd03mmc.json").read_text())
PLUG_SPEC = {"services": [{"iid": 2, "type": "urn:miot-spec-v2:service:switch:00007808:x:1", "description": "Switch",
    "properties": [{"iid": 1, "type": "urn:miot-spec-v2:property:on:00000006:x:1", "format": "bool", "access": ["read", "write", "notify"]}]},
    {"iid": 3, "type": "urn:miot-spec-v2:service:indicator-light:00007803:x:1", "description": "Indicator",
     "properties": [{"iid": 1, "type": "urn:miot-spec-v2:property:on:00000006:x:1", "format": "bool", "access": ["read", "write"]}]}]}


class Parsers(unittest.TestCase):
    def test_bthome(self):
        self.assertEqual(bp.parse_bthome(bytes.fromhex("4002c40903bf13")),
                         {"temperature": 25.0, "humidity": 50.55})
        self.assertEqual(bp.parse_bthome(bytes.fromhex("44004a01642d01")), {"battery": 100, "contact": True})
        self.assertEqual(bp.parse_bthome(bytes.fromhex("41aabb")), {"_encrypted": True})

    def test_atc(self):
        mac = bytes.fromhex("a4c138aabbcc")
        self.assertEqual(bp.parse_atc(mac + bytes.fromhex("00e2" "2d" "5a" "0bb8" "07")),
                         {"temperature": 22.6, "humidity": 45, "battery": 90, "voltage": 3.0})
        self.assertEqual(bp.parse_atc(mac[::-1] + bytes.fromhex("d408" "9411" "b80b" "5a" "07" "00")),
                         {"temperature": 22.6, "humidity": 45.0, "battery": 90, "voltage": 3.0})

    def test_mibeacon(self):
        self.assertEqual(bp.parse_mibeacon(bytes.fromhex("5020aa01b064aed0a8654c0d1004d9006001")),
                         {"temperature": 21.7, "humidity": 35.2})
        self.assertEqual(bp.parse_mibeacon(bytes.fromhex("5820aa01b064aed0a8654c0d1004d9006001")), {"_encrypted": True})
        fmt, vals = bp.parse_advert({bp.UUID_MI: bytes.fromhex("5020aa01b064aed0a8654c0a100164")})
        self.assertEqual((fmt, vals), ("Xiaomi", {"battery": 100}))


class Crypto(unittest.TestCase):
    def test_rc4_vector(self):
        self.assertEqual(xa.rc4(b"Key", b"Plaintext", drop=0).hex().upper(), "BBF316E8D940AF0AD3")

    def test_matches_reference(self):
        from Crypto.Cipher import ARC4

        def ref_enc(pw, payload):
            r = ARC4.new(base64.b64decode(pw)); r.encrypt(bytes(1024))
            return base64.b64encode(r.encrypt(payload.encode())).decode()

        def ref_sig(url, method, sn, params):
            p = [method.upper(), url.split("com")[1].replace("/app/", "/")]
            p += [f"{k}={v}" for k, v in params.items()] + [sn]
            return base64.b64encode(hashlib.sha1("&".join(p).encode()).digest()).decode()

        ssec = base64.b64encode(b"0123456789abcdef").decode()
        nonce = xa.gen_nonce(1700000000000)
        sn = xa.signed_nonce(ssec, nonce)
        url = "https://ru.api.io.mi.com/app/home/device_list"
        params = {"data": '{"a":1}'}
        params["rc4_hash__"] = ref_sig(url, "POST", sn, params)
        for k, v in params.items():
            params[k] = ref_enc(sn, v)
        params.update({"signature": ref_sig(url, "POST", sn, params), "ssecurity": ssec, "_nonce": nonce})
        self.assertEqual(xa.enc_params("/home/device_list", sn, nonce, {"data": '{"a":1}'}, ssec), params)


class Spec(unittest.TestCase):
    def test_sensor(self):
        m = map_spec(SPEC)
        self.assertEqual([(i["eid"], i["siid"], i["piid"]) for i in m],
                         [("temperature", 2, 1), ("humidity", 2, 2), ("battery", 3, 1)])

    def test_plug(self):
        m = map_spec(PLUG_SPEC)
        self.assertEqual(len(m), 1)
        self.assertTrue(m[0]["writable"] and m[0]["key"] == "switch")
        self.assertEqual(m[0]["name"], "Питание")

    def test_outlet_temperature_is_not_climate(self):
        spec = {"type": "urn:miot-spec-v2:device:outlet:0000A002:x:1", "services": [
            {"iid": 2, "type": "urn:miot-spec-v2:service:switch:00007808:x:1", "description": "Power Switch Status", "properties": [
                {"iid": 1, "type": "urn:miot-spec-v2:property:on:00000006:x:1", "format": "bool", "access": ["read", "write"]},
                {"iid": 6, "type": "urn:miot-spec-v2:property:temperature:00000020:x:1", "format": "uint8", "access": ["read"], "unit": "celsius"}]},
            {"iid": 7, "type": "urn:miot-spec-v2:service:usb-switch:00007858:x:1", "description": "USB Switch Status", "properties": [
                {"iid": 1, "type": "urn:miot-spec-v2:property:on:00000006:x:1", "format": "bool", "access": ["read", "write"]}]}]}
        m = {i["eid"]: i for i in map_spec(spec)}
        self.assertEqual(sorted(m), ["device_temp", "switch", "switch_2"])
        self.assertEqual((m["switch"]["name"], m["switch_2"]["name"]), ("Питание", "USB"))
        self.assertEqual([i["key"] for i in map_spec(SPEC)][:1], ["temperature"])


def fake_cloud(log):
    """Имитация облака Xiaomi: проверяет подпись, отвечает зашифрованно."""
    values = {("blt.3.abc", 2, 1): 21.5, ("blt.3.abc", 2, 2): 48.0, ("blt.3.abc", 3, 1): 77, ("plug1", 2, 1): False}

    async def handler(request):
        q = dict(request.query)
        path = request.path[len("/app"):]
        sn = xa.signed_nonce(q["ssecurity"], q["_nonce"])
        check = {k: v for k, v in q.items() if k in ("data", "rc4_hash__")}
        if request.headers.get("Cookie", "").find("serviceToken=TOKEN") < 0:
            return web.Response(status=401)
        assert xa.enc_signature(path, "POST", sn, check) == q["signature"], "bad signature"
        data = json.loads(xa.rc4(base64.b64decode(sn), base64.b64decode(q["data"])))
        log.append((path, data))
        if path == "/home/device_list":
            res = {"list": [
                {"did": "blt.3.abc", "name": "Термометр спальня", "model": "miaomiaoce.sensor_ht.t2", "isOnline": True,
                 "spec_type": SPEC["type"], "token": "SECRET"},
                {"did": "plug1", "name": "Розетка", "model": "x.plug.v1", "isOnline": True, "spec_type": "urn:plug"}]}
        elif path == "/v2/homeroom/gethome":
            res = {"homelist": [{"roomlist": [{"name": "Спальня", "dids": ["blt.3.abc"]}]}]}
        elif path == "/miotspec/prop/get":
            res = [{**p, "code": 0, "value": values[(p["did"], p["siid"], p["piid"])]} for p in data["params"]]
        elif path == "/miotspec/prop/set":
            for p in data["params"]:
                values[(p["did"], p["siid"], p["piid"])] = p["value"]
            res = [{"code": 0}]
        body = json.dumps({"code": 0, "message": "ok", "result": res})
        return web.Response(text=base64.b64encode(xa.rc4(base64.b64decode(sn), body.encode())).decode())

    app = web.Application()
    app.router.add_post("/app/{tail:.*}", handler)
    return app


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_xiaomi_driver(self):
        log = []
        srv = TestServer(fake_cloud(log)); await srv.start_server()
        tmp = Path(tempfile.mkdtemp())
        (tmp / "xiaomi_session.json").write_text(json.dumps({
            "userId": "42", "ssecurity": base64.b64encode(b"0123456789abcdef").decode(),
            "serviceToken": "TOKEN+/=", "countries": ["ru"]}))
        hub = Hub(tmp)
        drv = XiaomiDriver(hub, {"poll_seconds": 15}); hub.drivers["xiaomi"] = drv
        drv.cloud.api_url = lambda c: str(srv.make_url("/app"))

        async def spec(url):
            return SPEC if "miaomiaoce" in url else PLUG_SPEC
        drv._fetch_json = spec
        task = asyncio.create_task(drv.run())
        for _ in range(50):
            await asyncio.sleep(0.1)
            if drv.status == "ok":
                break
        self.assertEqual(drv.status, "ok", drv.message)
        snap = {d["id"]: d for d in hub.snapshot()["devices"]}
        t = snap["xiaomi:blt.3.abc"]
        self.assertEqual(t["room"], "Спальня")
        self.assertEqual({e["key"]: e["value"] for e in t["entities"]},
                         {"temperature": 21.5, "humidity": 48.0, "battery": 77})
        dev, ent = hub.find_entity("xiaomi:plug1/switch")
        self.assertIs(ent.value, False)
        await drv.set(dev, ent, True)
        self.assertEqual(log[-1], ("/miotspec/prop/set", {"params": [{"did": "plug1", "siid": 2, "piid": 1, "value": True}]}))
        self.assertNotIn("SECRET", json.dumps(drv.diagnostics()))
        task.cancel(); await drv.cloud.close(); await srv.close()

    async def test_shelly(self):
        st = {"on": False}

        async def h(request):
            p = request.path
            if p == "/shelly":
                return web.json_response({"gen": 2, "name": "Плита", "model": "SNSW-001P16EU"})
            if p == "/rpc/Shelly.GetStatus":
                return web.json_response({"switch:0": {"output": st["on"], "apower": 12.5, "temperature": {"tC": 40}},
                                          "sys": {}, "wifi": {}})
            if p == "/rpc/Switch.Set":
                st["on"] = request.query["on"] == "true"
                return web.json_response({"was_on": False})
        app = web.Application(); app.router.add_get("/{t:.*}", h)
        srv = TestServer(app); await srv.start_server()
        host = f"{srv.host}:{srv.port}"
        hub = Hub(Path(tempfile.mkdtemp()))
        drv = ShellyDriver(hub, {"hosts": [host], "poll_seconds": 5}); hub.drivers["shelly"] = drv
        task = asyncio.create_task(drv.run())
        for _ in range(30):
            await asyncio.sleep(0.1)
            if drv.status == "ok":
                break
        dev, ent = hub.find_entity(f"shelly:{host}/switch")
        self.assertIs(ent.value, False)
        self.assertEqual(dev.entities["power"].value, 12.5)
        await drv.set(dev, ent, True)
        self.assertTrue(st["on"])
        task.cancel(); await srv.close()

    async def test_server_api(self):
        tmp = Path(tempfile.mkdtemp())
        app = make_app({"access_key": "k1", "sources": {"xiaomi": False}}, tmp)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/state")
            self.assertEqual((r.status, r.headers["Access-Control-Allow-Origin"]), (401, "*"))
            r = await c.options("/api/set", headers={"Origin": "https://x.github.io", "Access-Control-Request-Method": "POST"})
            self.assertEqual((r.status, r.headers["Access-Control-Allow-Headers"]), (204, "Content-Type, X-Key"))
            H = {"X-Key": "k1"}
            await asyncio.sleep(0.3)
            s = await (await c.get("/api/state", headers=H)).json()
            self.assertEqual(len(s["devices"]), 7)
            r = await c.post("/api/set", headers=H, json={"entity": "demo:plug/switch", "value": False})
            self.assertEqual(r.status, 200)
            r = await c.post("/api/set", headers=H, json={"entity": "demo:living/temperature", "value": 1})
            self.assertEqual(r.status, 400)
            await c.post("/api/device", headers=H, json={"id": "demo:living", "name": "Зал", "room": "Зал"})
            s = await (await c.get("/api/state", headers=H)).json()
            d = {x["id"]: x for x in s["devices"]}
            self.assertEqual((d["demo:living"]["name"], d["demo:living"]["room"]), ("Зал", "Зал"))
            self.assertIs(next(e for e in d["demo:plug"]["entities"] if e["key"] == "switch")["value"], False)
            h = await (await c.get("/api/history?entity=demo:living/temperature", headers=H)).json()
            self.assertTrue(len(h) >= 1)
            self.assertEqual(d["demo:living"]["category"], "temperature-humidity-sensor")
            self.assertIsInstance(await (await c.get("/api/sparks", headers=H)).json(), dict)
            self.assertEqual((await c.get("/")).status, 200)
            r = await c.get("/api/events?key=k1")
            line = await r.content.readline()
            self.assertTrue(line.startswith(b"data: {"))
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
            r.close()


if __name__ == "__main__":
    unittest.main()
