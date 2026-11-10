#!/usr/bin/env bash
# Установка хаба на сервер Linux (Ubuntu/Debian). Запуск: bash install.sh
# Повторный запуск обновляет компоненты и перезапускает хаб; настройки и данные сохраняются.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PORT="${PORT:-8377}"

if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  echo "Ставлю python3-venv..."
  sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
# bleak (Bluetooth) на сервере не нужен
.venv/bin/pip install -q aiohttp pyyaml broadlink

if [ ! -f config.yaml ]; then
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  cat > config.yaml <<CFG
port: $PORT
host: 0.0.0.0          # после настройки nginx с https замените на 127.0.0.1
access_key: "$KEY"
sources:
  xiaomi:
    country: auto
  demo:
    mode: auto
CFG
  chmod 600 config.yaml
fi

sudo tee /etc/systemd/system/sensor-hub.service >/dev/null <<UNIT
[Unit]
Description=Sensor hub (datchiki)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m hub
Restart=always
RestartSec=5
User=$(id -un)
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable -q sensor-hub
sudo systemctl restart sensor-hub
sleep 2

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Хаб запущен."
echo "  Адрес панели:  http://$IP:$(grep '^port:' config.yaml | awk '{print $2}')"
echo "  Ключ доступа:  $(grep '^access_key:' config.yaml | cut -d'"' -f2)"
echo "Состояние: systemctl status sensor-hub    Журнал: journalctl -u sensor-hub -n 50"
