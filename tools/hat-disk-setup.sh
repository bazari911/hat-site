#!/usr/bin/env bash
# Хат Диск — установка и обслуживание. Запускать от root:
#   bash /opt/hat-site/tools/hat-disk-setup.sh ЭТАП
# Этапы:
#   install     — всё сразу: секрет, настройка почты, служба hat-disk, nginx /disk/, обновление темы
#   admin [N]   — предупреждения о месте в Telegram (берёт ваш Telegram из ящика, подтверждённого ботом)
#   alert-test  — прислать пробное предупреждение (Telegram + почта)
#   restart     — перезапустить службу после обновления кода (git pull)
#   test        — проверить всё целиком
#   usage       — кто сколько занял на Хат Диске
#   logs        — последние записи журнала службы
set -euo pipefail

MC=/opt/mailcow-dockerized
SITE=/opt/hat-site
CONF=/etc/hat-reg/config.json
PY="/usr/bin/python3 $SITE/tools/hat_disk.py"
NGX=$MC/data/conf/nginx/site.hatdisk.custom
RCCONF=$MC/data/web/rc/plugins/hat_theme/config.inc.php

[ "$(id -u)" = 0 ] || { echo "Запустите от root (sudo)"; exit 1; }
[ -f "$CONF" ] || { echo "Нет $CONF — сначала настройте регистрацию (hat-reg-setup.sh config)"; exit 1; }
step=${1:-help}

php_container() { docker ps -qf name=php-fpm-mailcow; }

case "$step" in
install)
  echo "== 1/5 секрет пропусков (disk_secret)"
  HAT_CONF="$CONF" python3 - <<'PYEOF'
import json, os, secrets
p = os.environ["HAT_CONF"]
cfg = json.load(open(p, encoding="utf-8"))
if cfg.get("disk_secret"):
    print("уже есть")
else:
    cfg["disk_secret"] = secrets.token_hex(32)
    tmp = p + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    print("создан (на экран не выводится)")
PYEOF

  echo "== 2/5 тема почты (hat_theme) и секрет для неё"
  bash "$SITE/tools/roundcube-install.sh" theme >/dev/null
  HAT_CONF="$CONF" HAT_RC="$RCCONF" python3 - <<'PYEOF'
import json, os
s = json.load(open(os.environ["HAT_CONF"], encoding="utf-8"))["disk_secret"]
p = os.environ["HAT_RC"]
with open(p, "w", encoding="utf-8") as f:
    f.write("<?php\n// Хат Диск: секрет пропусков (создан hat-disk-setup.sh, в git не хранится)\n"
            "$config['hat_disk_secret'] = '%s';\n" % s)
print("записан", p)
PYEOF
  PHP=$(php_container)
  docker exec "$PHP" chown root:www-data /web/rc/plugins/hat_theme/config.inc.php
  docker exec "$PHP" chmod 640 /web/rc/plugins/hat_theme/config.inc.php
  docker exec "$PHP" php -l /web/rc/plugins/hat_theme/config.inc.php
  docker exec "$PHP" php -l /web/rc/plugins/hat_theme/hat_theme.php
  (cd "$MC" && docker compose restart php-fpm-mailcow >/dev/null 2>&1) && echo "php-fpm перезапущен"

  echo "== 3/5 служба hat-disk"
  install -m 644 "$SITE/tools/hat-disk.service" /etc/systemd/system/hat-disk.service
  systemctl daemon-reload
  systemctl enable --now hat-disk
  systemctl restart hat-disk
  sleep 3
  systemctl is-active hat-disk
  journalctl -u hat-disk --no-pager -n 3 -o cat

  echo "== 4/5 nginx: адрес https://mail.kazxat.kz/disk/"
  BAK=""
  if [ -f "$NGX" ]; then BAK="$NGX.bak-$(date +%Y%m%d-%H%M%S)"; cp -a "$NGX" "$BAK"; echo "копия: $BAK"; fi
  cat > "$NGX" <<'EOCONF'
# Хат Диск (служба hat-disk на хосте): большие файлы в письмах ссылкой
location /disk/ {
  proxy_pass http://172.22.1.1:8791/disk/;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_http_version 1.1;
  proxy_request_buffering off;
  proxy_buffering off;
  client_max_body_size 2100m;
  client_body_timeout 3600s;
  proxy_send_timeout 3600s;
  proxy_read_timeout 3600s;
}
EOCONF
  cd "$MC"
  if OUT=$(docker compose exec -T nginx-mailcow nginx -t 2>&1); then
    echo "$OUT" | tail -1
    docker compose exec -T nginx-mailcow nginx -s reload && echo "nginx перечитал настройки"
  else
    echo "$OUT" | tail -3
    if [ -n "$BAK" ]; then cp -a "$BAK" "$NGX"; else rm -f "$NGX"; fi
    echo "nginx -t не прошёл — вернул как было"; exit 1
  fi

  echo "== 5/5 проверка"
  exec bash "$SITE/tools/hat-disk-setup.sh" test
  ;;
admin)
  $PY admin-tg ${2:-}
  ;;
alert-test)
  $PY alert-test
  ;;
restart)
  systemctl restart hat-disk
  sleep 3
  systemctl is-active hat-disk
  journalctl -u hat-disk --no-pager -n 5 -o cat
  ;;
test)
  $PY selftest || true
  echo "--- служба:"; systemctl is-active hat-disk || true
  echo "--- секрет в почте:"; [ -f "$RCCONF" ] && grep -c hat_disk_secret "$RCCONF" >/dev/null && echo "есть" || echo "НЕТ (запустите install)"
  echo "--- из контейнера nginx к службе:"
  (cd "$MC" && docker compose exec -T nginx-mailcow sh -c 'wget -qO- http://172.22.1.1:8791/disk/ping || curl -s http://172.22.1.1:8791/disk/ping') \
    || echo "НЕ ДОХОДИТ. Если включён ufw: ufw allow in on br-mailcow to 172.22.1.1 port 8791 proto tcp"
  echo; echo "--- снаружи через сайт:"
  curl -s https://mail.kazxat.kz/disk/ping; echo
  ;;
usage)
  $PY usage
  ;;
logs)
  journalctl -u hat-disk --no-pager -n "${2:-40}" -o short-iso
  ;;
*)
  sed -n '2,13p' "$0"
  ;;
esac
