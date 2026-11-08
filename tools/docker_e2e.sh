#!/usr/bin/env bash
# End to end inside the image (read-only root, no capabilities): a web server in another container is checked, goes
# away, and the status, the incidents and the page are read from the same volume. Needs Docker.
# Usage: tools/docker_e2e.sh [image]
set -euo pipefail
IMAGE="${1:-upstatus}"
ID="$$"
VOL="upstatus-e2e-$ID"
NET="upstatus-e2e-net-$ID"
SRV="upstatus-e2e-srv-$ID"
TMP="$(mktemp -d)"
trap 'docker rm -f "$SRV" >/dev/null 2>&1 || true; docker volume rm -f "$VOL" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT

[ "$(docker run --rm --entrypoint id "$IMAGE" -u)" != 0 ]   # not root
docker network create "$NET" >/dev/null
docker volume create "$VOL" >/dev/null
docker run --rm --user 0 -v "$VOL":/data --entrypoint chown "$IMAGE" 10001 /data
docker run -d --name "$SRV" --network "$NET" --entrypoint python "$IMAGE" -m http.server 8000 --directory /tmp >/dev/null
sleep 2

cat > "$TMP/upstatus.toml" <<TOML
[site]
title = "E2E status"

[defaults]
fail_after = 2
recover_after = 1

[[check]]
name = "Web"
group = "Test"
type = "http"
url = "http://$SRV:8000/"
contains = "Directory listing"

[[check]]
name = "Port"
group = "Test"
type = "tcp"
host = "$SRV"
port = 8000

[[check]]
name = "Resolver"
group = "Test"
type = "dns"
host = "localhost"
TOML
chmod 644 "$TMP/upstatus.toml"
docker run --rm --user 0 -v "$VOL":/data -v "$TMP/upstatus.toml":/in.toml:ro --entrypoint sh "$IMAGE" -c 'cat /in.toml > /data/upstatus.toml && chmod 644 /data/upstatus.toml'

RUN=(docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp --network "$NET" -v "$VOL":/data "$IMAGE")
"${RUN[@]}" validate | tee /dev/stderr | grep -q 'ok: 3 check'
"${RUN[@]}" run | tee /dev/stderr | grep -q 'ran 3 check'
"${RUN[@]}" status | tee /dev/stderr | grep -q '^Web: up'
"${RUN[@]}" status | grep -c ': up' | grep -q '^3$'
"${RUN[@]}" run | grep -q 'ran 0 check'                                            # nothing is due yet

docker run --rm --entrypoint cat -v "$VOL":/data "$IMAGE" /data/public/index.html > "$TMP/index.html"
grep -q 'All systems operational' "$TMP/index.html"
! grep -q -e "$SRV" -e ':8000' "$TMP/index.html"                                  # the page shows names, never hosts
! grep -qi '<script' "$TMP/index.html"

# the web server goes away: two failed checks make it an outage; the resolver still works
docker rm -f "$SRV" >/dev/null
"${RUN[@]}" run --all >/dev/null || true
if "${RUN[@]}" run --all >/dev/null; then echo "expected exit 1 when a service is down" >&2; exit 1; fi
"${RUN[@]}" status | tee /dev/stderr | grep -q '^Web: DOWN'
"${RUN[@]}" status | grep -q '^Resolver: up'
"${RUN[@]}" incidents | tee /dev/stderr | grep -q 'Web: still going'
docker run --rm --entrypoint cat -v "$VOL":/data "$IMAGE" /data/public/status.json | grep -q '"status": "down"'
docker run --rm --entrypoint cat -v "$VOL":/data "$IMAGE" /data/public/index.html | grep -q 'Partial outage'

# a mistake in the configuration is exit code 2
set +e; "${RUN[@]}" --config /nonexistent.toml validate >/dev/null 2>&1; rc=$?; set -e
[ "$rc" = 2 ]
echo "docker end-to-end: OK"
