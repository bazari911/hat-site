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
  rm -f $RC/public_html/installer.php
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

theme)
  echo "== theme: update hat_theme from $TPL/hat_theme + restart php-fpm"
  [ -d $RC/plugins/hat_theme ] && cp -a $RC/plugins/hat_theme /root/hat_theme.bak.$(date +%Y%m%d-%H%M%S) && echo "backup: /root/hat_theme.bak.*"
  cp -r $TPL/hat_theme/. $RC/plugins/hat_theme/
  ls -l $RC/plugins/hat_theme
  docker compose restart php-fpm-mailcow
  ;;

pwinfo)
  echo "== pwinfo: read-only facts for the password plugin (no secrets printed)"
  echo "HTTP_PORT=${HTTP_PORT} HTTP_BIND=${HTTP_BIND} HTTP_REDIRECT=${HTTP_REDIRECT:-n} IPV4_NETWORK=${IPV4_NETWORK}"
  echo "API_KEY in mailcow.conf: $([ -n "${API_KEY:-}" ] && echo set || echo empty); API_ALLOW_FROM=${API_ALLOW_FROM:-}"
  sql mailcow -e "SHOW COLUMNS FROM api;" | cut -f1 | tr '\n' ' '; echo
  sql mailcow -e "SELECT access, allow_from, skip_ip_check, active, LENGTH(api_key) AS keylen FROM api;" || true
  echo "php-fpm IP: $(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' $PHP)"
  grep -n "plugins'\]" $C
  ls -l $RC/plugins/password/drivers/mailcow.php $RC/plugins/password/config.inc.php.dist $RC/plugins/password/config.inc.php 2>&1 || true
  for U in http://nginx:${HTTP_PORT}/api/v1/get/status/version http://nginx/api/v1/get/status/version; do
    echo "$U -> $(docker exec $PHP curl -s -o /dev/null -w '%{http_code} %{redirect_url}' $U 2>&1 || echo curl-failed)"
  done
  ;;

password)
  echo "== password: Roundcube password plugin with mailcow driver"
  P=$RC/plugins/password
  PC=$P/config.inc.php
  [ -f $P/drivers/mailcow.php ] && [ -f $TPL/password.config.inc.php.template ] || { echo "STOP: plugin or template missing"; exit 1; }
  KEY=$(sql -N -s mailcow -e "SELECT api_key FROM api WHERE access='rw' AND active=1 AND skip_ip_check=0 LIMIT 1;")
  [ -n "$KEY" ] || { echo "STOP: no active read-write API key (create it in mailcow admin)"; exit 1; }
  echo "read-write API key found (${#KEY} chars, not shown)"
  if [ "${HTTP_REDIRECT:-n}" = "y" ]; then APIHOST="https://${MAILCOW_HOSTNAME}"; else APIHOST="http://nginx"; fi
  echo "API host: $APIHOST"
  bak $PC
  cp $TPL/password.config.inc.php.template $PC
  sed -i "/api_host/ s#__APIHOST__#$APIHOST#; /api_token/ s#__APIKEY__#$KEY#" $PC
  docker exec $PHP chown root:www-data /web/rc/plugins/password/config.inc.php
  docker exec $PHP chmod 640 /web/rc/plugins/password/config.inc.php
  echo "placeholders left in api_host/api_token lines (must be 0): $(grep -E 'api_host|api_token' $PC | grep -c '__' || true)"
  ls -l $PC
  if grep "plugins'\]" $C | grep -q "'password'"; then echo "'password' already in plugins"; else
    bak $C
    sed -i "/\$config\['plugins'\]/ s/\];/, 'password'];/" $C
  fi
  grep -n "plugins'\]" $C
  docker exec $PHP php -l /web/rc/config/config.inc.php
  docker exec $PHP php -l /web/rc/plugins/password/config.inc.php
  echo "API test from php-fpm with the key: HTTP $(docker exec $PHP curl -s -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" $APIHOST/api/v1/get/status/version || echo curl-failed)"
  docker compose restart php-fpm-mailcow
  ;;

apitest)
  echo "== apitest: read-only check of the mailcow API from php-fpm (key not shown)"
  KEY=$(sql -N -s mailcow -e "SELECT api_key FROM api WHERE access='rw' AND active=1 LIMIT 1;")
  sql mailcow -e "SELECT access, allow_from, skip_ip_check, active FROM api;"
  echo "DNS of ${MAILCOW_HOSTNAME} inside php-fpm: $(docker exec $PHP getent ahosts ${MAILCOW_HOSTNAME} | cut -d' ' -f1 | sort -u | tr '\n' ' ')"
  for U in https://${MAILCOW_HOSTNAME} https://nginx; do
    echo "$U -> HTTP $(docker exec $PHP curl -sk -o /dev/null -w '%{http_code} (connected to %{remote_ip})' -H "X-API-Key: $KEY" $U/api/v1/get/status/version 2>&1 || echo curl-failed)"
  done
  sleep 1
  echo "nginx saw these clients for status/version (last 4):"
  docker compose logs --since 2m nginx-mailcow 2>/dev/null | grep 'status/version' | tail -4 | cut -c1-120
  ;;

netinfo)
  CAND=${2:-247}
  echo "== netinfo: read-only, candidate fixed address for nginx: ${IPV4_NETWORK}.${CAND}"
  echo "-- fixed addresses in docker-compose.yml (service: address):"
  awk '/^  [a-z0-9-]+:$/{s=$1} /ipv4_address/{print "   " s, $2}' docker-compose.yml
  echo "-- fixed addresses in docker-compose.override.yml:"; grep -n ipv4_address docker-compose.override.yml || echo "   none"
  echo "-- network section of docker-compose.yml:"
  awk '/^networks:/{p=1} p' docker-compose.yml | head -20
  NET=$(docker network ls --format '{{.Name}}' | grep mailcow-network | head -1)
  echo "-- containers in $NET now:"
  docker network inspect $NET --format '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}' | sort -t. -k4 -n
  if docker network inspect $NET --format '{{range .Containers}}{{.IPv4Address}} {{end}}' | grep -qw "${IPV4_NETWORK}.${CAND}/24"; then echo "RESULT: ${IPV4_NETWORK}.${CAND} is IN USE now"; else echo "RESULT: ${IPV4_NETWORK}.${CAND} is not used by a running container"; fi
  if grep -q "${CAND}\$" <(awk '/ipv4_address/{print $2}' docker-compose.yml); then echo "RESULT: .${CAND} is RESERVED in docker-compose.yml"; else echo "RESULT: .${CAND} is not reserved in docker-compose.yml"; fi
  ;;

pinnginx)
  echo "== pinnginx: fixed address .247 for nginx, ${MAILCOW_HOSTNAME} -> nginx inside php-fpm only"
  O=docker-compose.override.yml
  if grep -q 'ipv4_address' $O; then echo "already done"; else
    B=$O.bak.$(date +%Y%m%d-%H%M%S); cp -a $O $B; echo "backup: $B"
    sed -i '/^  nginx-mailcow:$/a\    networks:\n      mailcow-network:\n        ipv4_address: ${IPV4_NETWORK:-172.22.1}.247' $O
    sed -i '/^  php-fpm-mailcow:$/a\    extra_hosts:\n      - "'"${MAILCOW_HOSTNAME}"':${IPV4_NETWORK:-172.22.1}.247"' $O
    if ! docker compose config -q; then echo "FAIL: compose config invalid, restoring $B"; cp -a $B $O; exit 1; fi
  fi
  cat $O
  docker compose config -q && echo "docker compose config OK"
  echo "-- merged nginx network settings:"
  docker compose config | awk '/^  nginx-mailcow:/{p=1;next} /^  [a-z]/{p=0} p' | grep -A5 '^    networks:'
  echo "-- merged php-fpm extra_hosts:"
  docker compose config | awk '/^  php-fpm-mailcow:/{p=1;next} /^  [a-z]/{p=0} p' | grep -A2 'extra_hosts'
  echo "now run: restart, then pincheck"
  ;;

pincheck)
  echo "== pincheck: read-only, after restart"
  echo "nginx IP: $(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' $NG)"
  echo "${MAILCOW_HOSTNAME} inside php-fpm: $(docker exec $PHP getent hosts ${MAILCOW_HOSTNAME})"
  KEY=$(sql -N -s mailcow -e "SELECT api_key FROM api WHERE access='rw' AND active=1 LIMIT 1;")
  echo "API with key, certificate VERIFIED: $(docker exec $PHP curl -s -o /dev/null -w 'HTTP %{http_code}, connected to %{remote_ip}, ssl_verify=%{ssl_verify_result}' -H "X-API-Key: $KEY" https://${MAILCOW_HOSTNAME}/api/v1/get/status/version 2>&1 || echo curl-failed)"
  grep -h password_mailcow_api_host $RC/plugins/password/config.inc.php
  ;;

quotainfo)
  D=${2:-kazxat.kz}
  echo "== quotainfo: read-only, domain $D"
  echo "-- disk /:"; df -h /
  echo "-- mail volumes:"; du -sh /var/lib/docker/volumes/*vmail* 2>/dev/null || echo "   none found"
  echo "-- domain settings (GB):"
  sql mailcow -e "SELECT domain, ROUND(quota/1073741824,2) AS domain_quota_gb, ROUND(defquota/1073741824,2) AS default_mailbox_gb, ROUND(maxquota/1073741824,2) AS max_mailbox_gb, mailboxes AS max_mailboxes, aliases AS max_aliases, active FROM domain WHERE domain='$D';"
  echo "-- mailboxes (quota GB, used MB):"
  sql mailcow -e "SELECT m.username, ROUND(m.quota/1073741824,2) AS quota_gb, ROUND(IFNULL(q.bytes,0)/1048576,1) AS used_mb, IFNULL(q.messages,0) AS msgs, m.active FROM mailbox m LEFT JOIN quota2 q ON q.username=m.username WHERE m.domain='$D' ORDER BY m.username;"
  echo "-- postfix message_size_limit: $(docker exec $(docker ps -qf name=postfix-mailcow) postconf -h message_size_limit) bytes"
  echo "-- mailcow.conf size settings:"; grep -n -i -E 'MAX_BODY|SIZE' mailcow.conf || echo "   none"
  echo "-- nginx client_max_body_size:"; docker exec $NG sh -c "grep -rh client_max_body_size /etc/nginx 2>/dev/null | sort -u" || true
  echo "-- php-fpm upload limits: $(docker exec $PHP php -r 'echo "upload_max_filesize=",ini_get("upload_max_filesize")," post_max_size=",ini_get("post_max_size");')"
  echo "-- roundcube max_message_size: $(grep -h max_message_size $C $RC/config/defaults.inc.php 2>/dev/null | head -2 | tr '\n' ' ')"
  ;;

msgsize)
  SIZE=${2:-36700160}; RCSIZE=${3:-35M}
  echo "== msgsize: Postfix message_size_limit=$SIZE, Roundcube max_message_size=$RCSIZE"
  X=data/conf/postfix/extra.cf
  bak $X; touch $X
  if grep -q '^message_size_limit' $X; then sed -i "s/^message_size_limit.*/message_size_limit = $SIZE/" $X; else echo "message_size_limit = $SIZE" >> $X; fi
  echo "-- $X:"; cat $X
  bak $C
  if grep -qF "['max_message_size']" $C; then sed -i "/^.config\['max_message_size'\]/ s/=.*/= '$RCSIZE';/" $C; else echo "\$config['max_message_size'] = '$RCSIZE';" >> $C; fi
  grep -n max_message_size $C
  docker exec $PHP php -l /web/rc/config/config.inc.php
  docker compose restart postfix-mailcow
  sleep 5
  echo "postfix message_size_limit now: $(docker exec $(docker ps -qf name=postfix-mailcow) postconf -h message_size_limit)"
  ;;

restart)
  echo "== restart mailcow (webmail down for 1-2 minutes)"
  docker compose down && docker compose up -d
  docker compose ps --format '{{.Service}}: {{.State}}'
  ;;

mailboxes)
  sql mailcow -e "SELECT username, active FROM mailbox WHERE domain='kazxat.kz';"
  ;;

*) echo "usage: $0 a|b|c|d|e|f|g|h|theme|pwinfo|password|apitest|netinfo|pinnginx|pincheck|quotainfo|msgsize|restart|mailboxes" ;;
esac
