#!/bin/bash
# Roundcube for mailcow (kazxat.kz), by the official mailcow guide (integrated install):
# https://docs.mailcow.email/third_party/roundcube/third_party-roundcube/
# Run as root, one stage at a time:  bash /opt/hat-site/tools/roundcube-install.sh <stage>
# Stages: a b c d  (then Roundcube installer in the browser)  e f g h  (then, after approval) restart
# Extra:  mailboxes  - list kazxat.kz mailboxes
# No passwords or keys are stored in this file or printed.
set -eo pipefail
cd /opt/mailcow-dockerized
source ./mailcow.conf
RCVER=1.7.6
TPL=/opt/hat-site/roundcube
RC=data/web/rc
C=$RC/config/config.inc.php
PHP=$(docker ps -qf name=php-fpm-mailcow)
MY=$(docker ps -qf name=mysql-mailcow)
NG=$(docker ps -qf name=nginx-mailcow)

# backup: file.bak (or file.bak.DATE if .bak already exists)
bak() {
  [ -e "$1" ] || return 0
  if [ -e "$1.bak" ]; then cp -a "$1" "$1.bak.$(date +%Y%m%d-%H%M%S)"; else cp -a "$1" "$1.bak"; fi
  echo "backup: $1.bak"
}
rnd() { LC_ALL=C tr -dc A-Za-z0-9 </dev/urandom 2>/dev/null | head -c "$1" || true; }
sql() { docker exec "$MY" mysql -uroot -p"$DBROOT" "$@"; }
reload_nginx() { docker exec "$NG" nginx -t && docker exec "$NG" nginx -s reload && echo "nginx reloaded"; }

case "$1" in
a)
  echo "== a: close /roundcube/ on kazxat.kz (hat-site.conf)"
  F=data/conf/nginx/hat-site.conf
  if grep -q 'location \^~ /roundcube/' $F; then echo "already done"; else
    bak $F
    sed -i '/location \^~ \/tools\/ { deny all; }/a\  location ^~ /roundcube/ { deny all; }' $F
    grep -q 'location \^~ /roundcube/' $F || { echo "FAIL: line not added (no tools line found?)"; exit 1; }
  fi
  grep -n 'git\|tools\|roundcube' $F
  reload_nginx
  ;;

b)
  echo "== b: download Roundcube $RCVER into $RC"
  mkdir -p -m 755 $RC
  wget -q -O - https://github.com/roundcube/roundcubemail/releases/download/$RCVER/roundcubemail-$RCVER-complete.tar.gz \
    | tar -xz --no-same-owner -C $RC --strip-components=1 -f -
  docker exec $PHP chown www-data:www-data /web/rc/logs /web/rc/temp
  docker exec $PHP chown root:www-data /web/rc/config
  docker exec $PHP chmod 750 /web/rc/logs /web/rc/temp /web/rc/config
  echo "spellcheck (aspell): not installed, as requested"
  wget -q -O $RC/config/mime.types http://svn.apache.org/repos/asf/httpd/httpd/trunk/docs/conf/mime.types
  ls -ld $RC $RC/public_html $RC/config/mime.types
  echo "version: $(grep -o "RCMAIL_VERSION', '[^']*" $RC/program/include/iniset.php | cut -d"'" -f3)"
  ;;

c)
  echo "== c: database roundcubemail + config.inc.php from template"
  [ -d $RC/config ] || { echo "STOP: run stage b first"; exit 1; }
  DBROUNDCUBE=$(rnd 28); DESKEY=$(rnd 32)
  [ ${#DBROUNDCUBE} -eq 28 ] && [ ${#DESKEY} -eq 32 ] || { echo "FAIL: random generation"; exit 1; }
  sql -e "CREATE DATABASE IF NOT EXISTS roundcubemail CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
  sql -e "CREATE USER IF NOT EXISTS 'roundcube'@'%' IDENTIFIED BY '${DBROUNDCUBE}';"
  sql -e "ALTER USER 'roundcube'@'%' IDENTIFIED BY '${DBROUNDCUBE}';"
  sql -e "GRANT ALL PRIVILEGES ON roundcubemail.* TO 'roundcube'@'%';"
  bak $C
  cp $TPL/config.inc.php.template $C
  sed -i "/db_dsnw/ s/__DBPASS__/$DBROUNDCUBE/; /des_key/ s/__DESKEY__/$DESKEY/" $C
  docker exec $PHP chown root:www-data /web/rc/config/config.inc.php
  docker exec $PHP chmod 640 /web/rc/config/config.inc.php
  echo "placeholders left in db_dsnw/des_key lines (must be 0): $(grep -E "db_dsnw|des_key'\]" $C | grep -c '__' || true)"
  ls -l $C
  ;;

d)
  echo "== d: hat_theme plugin + nginx /rc/ (site.roundcube.custom)"
  cp -r $TPL/hat_theme $RC/plugins/
  ls -l $RC/plugins/hat_theme
  N=data/conf/nginx/site.roundcube.custom
  bak $N
  cat <<EOCONFIG >$N
location ^~ /rc/ {
  alias /web/rc/public_html/;
  index index.php;

  location ~ ^/rc/(?<rcfile>[^/]+\.php)(?<rcpathinfo>/.*)?\$ {
    alias /web/rc/public_html/\$rcfile;
    fastcgi_param SCRIPT_FILENAME /web/rc/public_html/\$rcfile;
    fastcgi_param PATH_INFO \$rcpathinfo;
    fastcgi_pass phpfpm:9002;
    include /etc/nginx/fastcgi_params;
  }
}
EOCONFIG
  reload_nginx
  echo "now open https://${MAILCOW_HOSTNAME}/rc/installer/"
  ;;

e)
  echo "== e: remove installer, enable_installer = false"
  rm -rf $RC/installer
  [ -L $RC/public_html/installer ] && rm $RC/public_html/installer
  bak $C
  sed -i -e "s/\(\$config\['enable_installer'\].* = \)true/\1false/" $C
  grep enable_installer $C
  ;;

f)
  echo "== f: composer update + audit"
  [ -e $RC/composer.json ] || cp $RC/composer.json-dist $RC/composer.json
  docker exec -w /web/rc $PHP composer config --no-interaction allow-plugins.roundcube/plugin-installer true
  if ! docker exec -w /web/rc $PHP composer update --no-dev -o --no-interaction; then
    echo "retry after dump-autoload (known composer issue, see guide)"
    docker exec -w /web/rc $PHP composer dump-autoload -o
    docker exec -w /web/rc $PHP composer update --no-dev -o --no-interaction
  fi
  docker exec -w /web/rc $PHP composer audit --no-interaction || echo "(composer audit reported something, see above)"
  ;;

g)
  echo "== g: dovecot plaintext auth for the internal network (extra.conf)"
  E=data/conf/dovecot/extra.conf
  if grep -q 'disable_plaintext_auth = no' $E 2>/dev/null; then echo "already done"; else
    bak $E
    cat <<EOCONFIG >>$E
remote ${IPV4_NETWORK}.0/24 {
  disable_plaintext_auth = no
}
remote ${IPV6_NETWORK} {
  disable_plaintext_auth = no
}
EOCONFIG
  fi
  cat $E
  DOV=$(docker ps -qf name=dovecot-mailcow)
  if docker exec $DOV doveconf -n >/dev/null; then echo "dovecot config OK (applies after restart)"; else
    echo "FAIL: dovecot config invalid, restoring"; if [ -e $E.bak ]; then cp -a $E.bak $E; else rm -f $E; fi; exit 1; fi
  ;;

h)
  echo "== h: ofelia cleanup job in docker-compose.override.yml"
  O=docker-compose.override.yml
  if grep -q roundcube_cleandb $O; then echo "already done"; else
    bak $O
    cat <<'EOCONFIG' >>$O
  php-fpm-mailcow:
    labels:
      ofelia.enabled: "true"
      ofelia.job-exec.roundcube_cleandb.schedule: "@every 168h"
      ofelia.job-exec.roundcube_cleandb.user: "www-data"
      ofelia.job-exec.roundcube_cleandb.command: '/bin/bash -c "[ -f /web/rc/bin/cleandb.sh ] && /web/rc/bin/cleandb.sh"'
EOCONFIG
  fi
  cat $O
  if docker compose config -q; then echo "docker compose config OK"; else
    echo "FAIL: compose config invalid, restoring"; cp -a $O.bak $O; exit 1; fi
  ;;

restart)
  echo "== restart mailcow (webmail down for 1-2 minutes)"
  docker compose down && docker compose up -d
  docker compose ps --format '{{.Service}}: {{.State}}'
  ;;

mailboxes)
  sql mailcow -e "SELECT username, active FROM mailbox WHERE domain='kazxat.kz';"
  ;;

*) echo "usage: $0 a|b|c|d|e|f|g|h|restart|mailboxes" ;;
esac
