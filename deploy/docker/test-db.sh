#!/bin/sh
set -eu

name="hl-app-test-db-$$"
image="${HL_APP_TEST_IMAGE:-hl-app-web:local}"
docker run -d --name "$name" --tmpfs /var/lib/postgresql/data:rw,size=512m -e POSTGRES_PASSWORD=test postgres:16-alpine >/dev/null
cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

ready=false
for _attempt in $(seq 1 30); do
    if docker exec "$name" pg_isready -U postgres >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [ "$ready" != true ]; then
    echo "temporary PostgreSQL did not become ready" >&2
    exit 1
fi

ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$name")
if [ "$#" -eq 0 ]; then
    set -- -m db -q
fi
docker run --rm \
    -v "$(pwd)":/work -w /work \
    -e PYTHONPATH=/work/backend \
    -e HL_APP_TEST_DATABASE_URL="postgresql://postgres:test@$ip/postgres" \
    "$image" python -m pytest backend/tests "$@"
