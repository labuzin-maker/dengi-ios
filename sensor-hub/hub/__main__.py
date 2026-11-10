"""Запуск: python -m hub"""
import sys
from pathlib import Path

from aiohttp import web

from .server import ROOT, local_ip, make_app


def main():
    cfg = {}
    f = ROOT / "config.yaml"
    if f.exists():
        import yaml
        cfg = yaml.safe_load(f.read_text("utf-8")) or {}
    port = int(cfg.get("port", 8080))
    app = make_app(cfg, ROOT / "data")
    print("=" * 56)
    print(" Хаб датчиков запущен")
    host = str(cfg.get("host", "0.0.0.0"))
    print(f" На этом компьютере:  http://localhost:{port}")
    if host != "127.0.0.1":
        print(f" С телефона:          http://{local_ip()}:{port}")
    if host != "127.0.0.1" and not cfg.get("access_key"):
        print(" Ключ доступа не задан: панель открыта всем, кто видит этот адрес.")
    print(" Остановить: Ctrl+C")
    print("=" * 56, flush=True)
    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
