#!/usr/bin/env bash
# Хат — резервные копии сервера. Запускать от root:
#   bash /opt/hat-site/tools/hat-backup.sh ЭТАП
# Этапы:
#   run      — сделать копию сейчас (почта mailcow + наши службы и настройки)
#   install  — делать копию каждую ночь в 03:30 по Казахстану (cron) и сразу сделать первую
#   status   — какие копии есть, сколько места занято, последние строки журнала
#   test     — проверить, что копия читается (без восстановления)
#   restore-help — как восстановить из копии (только инструкция, ничего не меняет)
#
# Что копируется:
#   почта mailcow целиком (письма, база, настройки, ключи) — штатным скриптом mailcow;
#   регистрация (база hat-reg), Хат Диск (база; сами файлы не копируются — они живут 30 дней),
#   секреты /etc/hat-reg, настройки nginx/postfix/dovecot, Roundcube, службы systemd.
# Копии лежат в /opt/backups, права только у root. Хранится 3 последних ночи.
# При ошибке приходит сообщение в Telegram администратора и на support@kazxat.kz.
set -uo pipefail

MC=/opt/mailcow-dockerized
SITE=/opt/hat-site
ROOT=/opt/backups
KEEP_DAYS=3
LOG=/var/log/hat-backup.log
CRON=/etc/cron.d/hat-backup

[ "$(id -u)" = 0 ] || { echo "Запустите от root"; exit 1; }
step=${1:-help}

notify() { /usr/bin/python3 "$SITE/tools/hat_disk.py" notify "$1" >/dev/null 2>&1 || true; }
human() { du -sh "$1" 2>/dev/null | cut -f1; }

do_run() {
  local START=$(date +%s) STAMP=$(date +%Y-%m-%d-%H%M) FAIL=""
  mkdir -p "$ROOT/mailcow" "$ROOT/hat"
  chmod 700 "$ROOT"
  echo "== $(date '+%F %T') резервная копия начата"

  # 1. Место: нужна хотя бы двойная прошлая копия + 2 ГБ запаса
  local FREE_KB=$(df -Pk "$ROOT" | awk 'NR==2{print $4}')
  local LAST_KB=$(du -sk "$ROOT/mailcow" 2>/dev/null | cut -f1)
  if [ "$FREE_KB" -lt $(( ${LAST_KB:-0} / 2 + 2097152 )) ]; then
    FAIL="мало места на диске для копии (свободно $((FREE_KB/1048576)) ГБ)"
  fi

  # 2. Почта: штатный скрипт mailcow (всё сразу), старше KEEP_DAYS дней — удаляются
  if [ -z "$FAIL" ]; then
    echo "-- почта mailcow"
    if ! (cd "$MC" && MAILCOW_BACKUP_LOCATION="$ROOT/mailcow" THREADS=2 \
          ./helper-scripts/backup_and_restore.sh backup all --delete-days "$KEEP_DAYS" </dev/null); then
      FAIL="ошибка копии почты mailcow"
    fi
    # последняя копия должна содержать базу и письма
    local LASTDIR=$(ls -1d "$ROOT"/mailcow/mailcow-* 2>/dev/null | tail -1)
    if [ -z "$FAIL" ] && { [ -z "$LASTDIR" ] || [ -z "$(find "$LASTDIR" -maxdepth 1 -name 'backup_mariadb.tar.*' -size +0)" ] || [ -z "$(find "$LASTDIR" -maxdepth 1 -name 'backup_vmail.tar.*' -size +0)" ]; }; then
      FAIL="копия почты неполная (нет базы или писем)"
    fi
  fi

  # 3. Наши службы и настройки
  echo "-- службы Хат и настройки"
  local TMP=$(mktemp -d)
  # базы SQLite копируем безопасно (на ходу, без остановки служб)
  /usr/bin/python3 - "$TMP" <<'PYEOF' || FAIL="${FAIL:+$FAIL; }ошибка копии баз Хат"
import os, sqlite3, sys
out = sys.argv[1]
for name, cands in {"hat-reg.db": ["/var/lib/private/hat-reg/reg.db", "/var/lib/hat-reg/reg.db"],
                    "hat-disk.db": ["/var/lib/private/hat-disk/disk.db", "/var/lib/hat-disk/disk.db"]}.items():
    for p in cands:
        if os.path.exists(p):
            src = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
            dst = sqlite3.connect(os.path.join(out, name))
            src.backup(dst)
            dst.close(); src.close()
            print("  база", name, "OK")
            break
PYEOF
  local LIST=()
  for f in /etc/hat-reg \
           /etc/systemd/system/hat-reg.service /etc/systemd/system/hat-disk.service \
           /etc/cron.d/hat-news /etc/cron.d/hat-backup \
           "$MC/mailcow.conf" "$MC/docker-compose.override.yml" \
           "$MC/data/conf/nginx" "$MC/data/conf/postfix/extra.cf" "$MC/data/conf/dovecot/extra.conf" \
           "$MC/data/web/rc/config/config.inc.php" \
           "$MC/data/web/rc/plugins/hat_theme/config.inc.php" \
           "$MC/data/web/rc/plugins/password/config.inc.php"; do
    [ -e "$f" ] && LIST+=("$f")
  done
  local OUT="$ROOT/hat/hat-$STAMP.tar.gz"
  if tar -czf "$OUT" -C "$TMP" . -P "${LIST[@]}" 2>/dev/null; then
    chmod 600 "$OUT"
    echo "  $OUT ($(human "$OUT"))"
  else
    FAIL="${FAIL:+$FAIL; }ошибка архива настроек Хат"
  fi
  rm -rf "$TMP"
  find "$ROOT/hat" -name 'hat-*.tar.gz' -mtime +"$KEEP_DAYS" -delete

  local SEC=$(( $(date +%s) - START ))
  if [ -n "$FAIL" ]; then
    echo "== ОШИБКА: $FAIL ($SEC с)"
    notify "Резервная копия НЕ сделана: $FAIL. Подробности: bash tools/hat-backup.sh status"
    return 1
  fi
  echo "== $(date '+%F %T') готово за $SEC с. Почта: $(human "$ROOT/mailcow"), Хат: $(human "$ROOT/hat"), свободно: $(df -Ph "$ROOT" | awk 'NR==2{print $4}')"
  return 0
}

case "$step" in
run)
  do_run 2>&1 | tee -a "$LOG"
  exit "${PIPESTATUS[0]}"
  ;;
install)
  # 03:30 по Казахстану = 22:30 UTC (часы сервера идут по UTC)
  cat > "$CRON" <<EOCRON
# Хат: ночная резервная копия (03:30 по Казахстану)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 22 * * * root /bin/bash $SITE/tools/hat-backup.sh run >/dev/null 2>&1
EOCRON
  chmod 644 "$CRON"
  echo "Расписание: каждую ночь в 03:30 по Казахстану ($CRON)"
  echo "Делаю первую копию сейчас (несколько минут)..."
  do_run 2>&1 | tee -a "$LOG"
  exit "${PIPESTATUS[0]}"
  ;;
status)
  echo "Расписание: $( [ -f "$CRON" ] && grep -v '^#' "$CRON" | grep root || echo 'НЕ установлено (install)')"
  echo "-- копии почты:"; ls -1d "$ROOT"/mailcow/mailcow-* 2>/dev/null | while read -r d; do echo "   $(basename "$d")  $(human "$d")"; done
  echo "-- копии Хат:"; ls -1 "$ROOT"/hat/hat-*.tar.gz 2>/dev/null | while read -r f; do echo "   $(basename "$f")  $(human "$f")"; done
  echo "-- занято копиями: $(human "$ROOT"), свободно на диске: $(df -Ph / | awk 'NR==2{print $4}')"
  echo "-- журнал:"; tail -n 8 "$LOG" 2>/dev/null || echo "   пусто"
  ;;
test)
  LASTDIR=$(ls -1d "$ROOT"/mailcow/mailcow-* 2>/dev/null | tail -1)
  [ -n "$LASTDIR" ] || { echo "Копий почты нет"; exit 1; }
  echo "Последняя копия почты: $LASTDIR"
  for f in "$LASTDIR"/*.tar.*; do
    case "$f" in
      *.zst) CHK="zstd -t -q" ;;
      *.gz)  CHK="gzip -t" ;;
      *) continue ;;
    esac
    if $CHK "$f" 2>/dev/null || docker run --rm -v "$LASTDIR":/b:ro ghcr.io/mailcow/backup:latest $CHK "/b/$(basename "$f")" 2>/dev/null; then
      echo "   OK  $(basename "$f")  $(human "$f")"
    else
      echo "   ПЛОХО $(basename "$f")"
    fi
  done
  LASTHAT=$(ls -1 "$ROOT"/hat/hat-*.tar.gz 2>/dev/null | tail -1)
  if [ -n "$LASTHAT" ] && tar -tzf "$LASTHAT" >/dev/null 2>&1; then
    echo "Последняя копия Хат: $(basename "$LASTHAT") — читается, файлов: $(tar -tzf "$LASTHAT" | wc -l)"
  else
    echo "Копия Хат не читается или её нет"
  fi
  ;;
restore-help)
  cat <<'EOHELP'
Восстановление (ничего не делается автоматически — только по шагам, вручную):
 1. Почта: cd /opt/mailcow-dockerized && ./helper-scripts/backup_and_restore.sh restore
    папка копий: /opt/backups/mailcow — скрипт сам спросит, какую копию и что восстановить.
 2. Настройки и службы Хат: tar -tzf /opt/backups/hat/hat-ДАТА.tar.gz — посмотреть содержимое;
    нужный файл достать: tar -xzf /opt/backups/hat/hat-ДАТА.tar.gz -P путь/к/файлу
    базы регистрации и диска лежат внутри как ./hat-reg.db и ./hat-disk.db
    (перед заменой базы остановить службу: systemctl stop hat-reg).
 3. Код сайта и служб — в GitHub (bazari911/hat-site): git clone в /opt/hat-site.
EOHELP
  ;;
*)
  sed -n '2,16p' "$0"
  ;;
esac
