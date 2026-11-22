#!/usr/bin/env bash
# Хат — установка регистрации ящиков. Запускать по этапам:
#   sudo bash /opt/hat-site/tools/hat-reg-setup.sh ЭТАП
# Этапы (по порядку):
#   config    — создать /etc/hat-reg/config.json: спросит токен бота (ввод скрыт),
#               ключ API возьмёт из настроек смены пароля Roundcube
#   noreply   — создать ящик noreply@kazxat.kz для писем с кодами
#   limits N  — поднять лимит ящиков домена до N (без N — только показать)
#   service   — установить и запустить службу hat-reg
#   nginx     — добавить /api/reg/ в hat-site.conf (с копией и проверкой nginx -t)
#   test      — проверить всё целиком
#   logs      — последние записи журнала службы
set -euo pipefail

MC=/opt/mailcow-dockerized
SITE=/opt/hat-site
CONF_DIR=/etc/hat-reg
CONF=$CONF_DIR/config.json
PY="/usr/bin/python3 $SITE/tools/hat_reg.py"
NGX=$MC/data/conf/nginx/hat-site.conf
RCPASS=$MC/data/web/rc/plugins/password/config.inc.php

[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }
step=${1:-help}

case "$step" in
config)
  mkdir -p "$CONF_DIR"; chmod 700 "$CONF_DIR"
  [ -f "$RCPASS" ] || { echo "Не найден $RCPASS"; exit 1; }
  echo "Вставьте токен бота @kazxat_kz_bot из BotFather и нажмите Enter."
  echo "(Символы на экране не появятся — так и должно быть.)"
  read -rs TOKEN; echo
  if ! [[ "$TOKEN" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]]; then
    echo "Токен не похож на настоящий (вид: 1234567890:AAH...). Ничего не записано."; exit 1
  fi
  HAT_TOKEN="$TOKEN" HAT_RCPASS="$RCPASS" HAT_CONF="$CONF" python3 - <<'PYEOF'
import json, os, re, secrets
conf = os.environ["HAT_CONF"]
cfg = {}
if os.path.exists(conf):
    cfg = json.load(open(conf, encoding="utf-8"))
src = open(os.environ["HAT_RCPASS"], encoding="utf-8").read()
m = re.search(r"password_mailcow_api_token'\]\s*=\s*'([^']+)'", src)
if not m or "__" in m.group(1):
    raise SystemExit("Не нашёл ключ API в настройках Roundcube")
cfg["api_key"] = m.group(1)
cfg["bot_token"] = os.environ["HAT_TOKEN"]
cfg.setdefault("bot_name", "kazxat_kz_bot")
cfg.setdefault("salt", secrets.token_hex(32))
tmp = conf + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=1)
os.replace(tmp, conf)
print("Записано:", conf, "(права 600). Ключ API и токен на экран не выводились.")
PYEOF
  unset TOKEN
  $PY selftest || true
  ;;
noreply)
  $PY create-noreply
  ;;
limits)
  $PY domain-limits ${2:-}
  ;;
service)
  install -m 644 "$SITE/tools/hat-reg.service" /etc/systemd/system/hat-reg.service
  systemctl daemon-reload
  systemctl enable --now hat-reg
  sleep 3
  systemctl --no-pager --lines=0 status hat-reg | head -5
  journalctl -u hat-reg --no-pager -n 5 -o cat
  ;;
nginx)
  [ -f "$NGX" ] || { echo "Не найден $NGX"; exit 1; }
  if grep -q "hat-reg" "$NGX"; then echo "В hat-site.conf уже есть блок hat-reg"; exit 0; fi
  BAK="$NGX.bak-hatreg-$(date +%Y%m%d-%H%M%S)"
  cp -a "$NGX" "$BAK"; echo "Копия: $BAK"
  HAT_NGX="$NGX" python3 - <<'PYEOF'
import os, re
p = os.environ["HAT_NGX"]
s = open(p, encoding="utf-8").read()
block = """
    # hat-reg: регистрация ящиков (служба на хосте)
    location /api/reg/ {
        proxy_pass http://172.22.1.1:8790/api/reg/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 70s;
        client_max_body_size 16k;
    }
    # служебные скрипты сайта наружу не отдаём
    location ^~ /tools/ { return 404; }
"""
new, n = re.subn(r"(^[ \t]*root[ \t]+/hat-site/?;[^\n]*\n)", lambda m: m.group(1) + block, s, flags=re.M)
if n == 0:
    raise SystemExit("Не нашёл строку 'root /hat-site;' — ничего не менял")
open(p, "w", encoding="utf-8").write(new)
print("Блок добавлен в", n, "server{}")
PYEOF
  cd "$MC"
  if docker compose exec -T nginx-mailcow nginx -t; then
    docker compose exec -T nginx-mailcow nginx -s reload && echo "nginx перечитал настройки"
  else
    cp -a "$BAK" "$NGX"; echo "nginx -t не прошёл — вернул прежний файл"; exit 1
  fi
  ;;
test)
  $PY selftest || true
  echo "--- служба:"; systemctl is-active hat-reg || true
  echo "--- из контейнера nginx к службе:"
  (cd "$MC" && docker compose exec -T nginx-mailcow sh -c 'wget -qO- http://172.22.1.1:8790/api/reg/ping || curl -s http://172.22.1.1:8790/api/reg/ping') \
    || { echo "НЕ ДОХОДИТ. Если включён ufw: sudo ufw allow in on br-mailcow to 172.22.1.1 port 8790 proto tcp"; }
  echo; echo "--- снаружи через сайт:"
  curl -s https://kazxat.kz/api/reg/ping; echo
  ;;
logs)
  journalctl -u hat-reg --no-pager -n "${2:-40}" -o short-iso
  ;;
*)
  sed -n '2,15p' "$0"
  ;;
esac
