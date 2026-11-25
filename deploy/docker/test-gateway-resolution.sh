#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image=${GATEWAY_TEST_IMAGE:-nginx:1.27-alpine}
name=hl-gateway-resolution-$$
network=$name-network
gateway=$name-gateway
web_old=$name-web-old
web_new=$name-web-new

cleanup() {
    docker rm -f "$gateway" "$web_old" "$web_new" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

start_web() {
    container=$1
    shift
    docker run -d --name "$container" --network "$network" "$@" \
        --entrypoint sh "$image" \
        -c 'sed -i -E "s/listen[[:space:]]+80;/listen 8000;/" /etc/nginx/conf.d/default.conf && exec nginx -g "daemon off;"' >/dev/null
    docker exec "$container" sh -c 'mkdir -p /usr/share/nginx/html/api/v1 && printf ok > /usr/share/nginx/html/api/v1/health'
}

request_health() {
    docker exec "$gateway" wget -q -O - 'http://127.0.0.1/api/v1/health?source=resolution-test' 2>/dev/null
}

wait_for_health() {
    attempt=0
    while [ "$attempt" -lt 60 ]; do
        if [ "$(request_health || true)" = "ok" ]; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 0.5
    done
    return 1
}

container_ip() {
    docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1"
}

show_diagnostics() {
    docker exec "$gateway" getent hosts web >&2 || true
    docker exec "$gateway" wget -O - 'http://127.0.0.1/api/v1/health?source=diagnostics' >&2 || true
    docker logs "$gateway" >&2
    docker logs "$web_old" >&2 || true
    docker logs "$web_new" >&2 || true
}

docker network create "$network" >/dev/null
start_web "$web_old" --network-alias web
docker run -d --name "$gateway" --network "$network" \
    -v "$root/deploy/docker/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
    "$image" >/dev/null

if ! wait_for_health; then
    echo 'gateway did not reach the initial Web container' >&2
    show_diagnostics
    exit 1
fi
old_ip=$(container_ip "$web_old")

start_web "$web_new"
new_ip=$(container_ip "$web_new")
if [ "$old_ip" = "$new_ip" ]; then
    echo 'test setup did not produce a new Web IP' >&2
    exit 1
fi

docker rm -f "$web_old" >/dev/null
docker network disconnect "$network" "$web_new"
docker network connect --alias web "$network" "$web_new"

if ! wait_for_health; then
    echo "gateway did not follow Web from $old_ip to $new_ip without a reload" >&2
    show_diagnostics
    exit 1
fi

echo "gateway followed Web from $old_ip to $new_ip without a reload"
