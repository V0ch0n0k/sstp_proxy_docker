#!/bin/bash
# Поднимает SSTP-туннель, направляет весь трафик контейнера через ppp0
# и запускает Squid поверх него. Если VPN или Squid падает, скрипт
# завершается, а контейнер перезапускает restart-политика docker compose.
set -euo pipefail

VPN_LOG=/var/log/vpn/sstp.log
PPP_IF=ppp0
REPLY_TABLE=100

log() { echo "[starter] $*"; }
die() { log "ERROR: $*"; exit 1; }

: "${SSTP_SERVER:?SSTP_SERVER is not set}"
: "${USERNAME:?USERNAME is not set}"
: "${PASSWORD:?PASSWORD is not set}"
[ -c /dev/ppp ] || die "/dev/ppp not found: pass it with 'devices: [/dev/ppp]'"

mkdir -p /var/log/vpn /var/run/sstpc
touch "$VPN_LOG"
rm -f /etc/ppp/resolv.conf

# Исходный маршрут по умолчанию (docker bridge). Берём его из основной
# таблицы, а если там уже пусто (повторный запуск), то из REPLY_TABLE.
DEF_GW="" DEF_IF=""
read -r DEF_GW DEF_IF < <(
    { ip -4 route show default; ip -4 route show default table "$REPLY_TABLE" 2>/dev/null || true; } |
    awk 'NR == 1 { for (i = 1; i < NF; i++) { if ($i == "via") gw = $(i + 1); if ($i == "dev") dev = $(i + 1) }; print gw, dev }'
) || true
[ -n "$DEF_GW" ] && [ -n "$DEF_IF" ] || die "no default route in container"
DEF_IP=$(ip -4 -o addr show dev "$DEF_IF" | awk '{ split($4, a, "/"); print a[1]; exit }')
log "uplink: $DEF_IF ip=$DEF_IP gw=$DEF_GW"

# Всё, что отправляется с адреса eth0 (ответы клиентам прокси и сама
# SSTP-сессия), уходит через eth0. Новые исходящие соединения идут по
# основной таблице, то есть через туннель.
ip route replace default via "$DEF_GW" dev "$DEF_IF" table "$REPLY_TABLE"
ip rule del from "$DEF_IP" lookup "$REPLY_TABLE" 2>/dev/null || true
ip rule add from "$DEF_IP" lookup "$REPLY_TABLE" priority 100

SSTP_ARGS=(--log-stderr --log-level "${SSTP_LOG_LEVEL:-1}" --save-server-route --tls-ext)
if [ "${SSTP_IGNORE_CERT:-false}" = "true" ]; then
    SSTP_ARGS+=(--cert-warn)
fi
# shellcheck disable=SC2206 # дополнительные опции pppd разбиваются по словам
PPPD_ARGS=(noauth nodefaultroute usepeerdns ${SSTP_PPPD_OPTS:-})

log "connecting to $SSTP_SERVER"
sstpc "${SSTP_ARGS[@]}" --user "$USERNAME" --password "$PASSWORD" \
      "$SSTP_SERVER" "${PPPD_ARGS[@]}" > >(tee -a "$VPN_LOG") 2>&1 &
SSTP_PID=$!

TIMEOUT=${SSTP_CONNECT_TIMEOUT:-60}
PPP_IP=""
for _ in $(seq "$TIMEOUT"); do
    kill -0 "$SSTP_PID" 2>/dev/null || die "sstpc exited, see $VPN_LOG"
    PPP_IP=$(ip -4 -o addr show dev "$PPP_IF" 2>/dev/null | awk '{ split($4, a, "/"); print a[1]; exit }') || true
    [ -n "$PPP_IP" ] && break
    sleep 1
done
if [ -z "$PPP_IP" ]; then
    kill "$SSTP_PID" 2>/dev/null || true
    die "$PPP_IF did not come up in ${TIMEOUT}s"
fi
log "tunnel up: $PPP_IF ip=$PPP_IP"

ip route replace default dev "$PPP_IF"
log "default route -> $PPP_IF"

# DNS тоже через VPN: адреса от сервера (usepeerdns), иначе VPN_DNS.
# /etc/resolv.conf смонтирован докером, поэтому перезаписываем содержимое.
for _ in 1 2 3; do
    [ -s /etc/ppp/resolv.conf ] && break
    sleep 1
done
if [ -s /etc/ppp/resolv.conf ]; then
    cat /etc/ppp/resolv.conf > /etc/resolv.conf
else
    echo "nameserver ${VPN_DNS:-1.1.1.1}" > /etc/resolv.conf
fi
log "dns: $(awk '/^nameserver/ { printf "%s ", $2 }' /etc/resolv.conf)"

chown -R proxy:proxy /var/log/squid 2>/dev/null || true
rm -f /run/squid.pid
squid -N -z
squid -N -d 1 &
SQUID_PID=$!
log "squid started"

trap 'kill "$SSTP_PID" "$SQUID_PID" 2>/dev/null || true; exit 0' TERM INT
set +e
wait -n "$SSTP_PID" "$SQUID_PID"
set -e
log "sstpc or squid exited, stopping"
kill "$SSTP_PID" "$SQUID_PID" 2>/dev/null || true
exit 1
