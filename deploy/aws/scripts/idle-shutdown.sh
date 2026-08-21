#!/bin/bash
# Copyright 2026 — OpenVPS AWS deployment
# SPDX-License-Identifier: MIT
#
# Stops the GPU instance once nothing has used it for IDLE_MINUTES.
#
# Installed by the CloudFormation template as /usr/local/bin/openvps-idle-shutdown and
# driven by openvps-idle.timer. Threshold comes from the IdleShutdownMinutes stack
# parameter via /opt/openvps/idle.conf; 0 disables shutdown entirely.
#
# The instance is *stopped*, not terminated: the root volume keeps the built images and the
# HLOC cache, and the maps volume is a separate resource that is never in question. Waking
# it is the waker's StartInstances call.
#
# Idle means all four of these are true:
#
#   1. No CUDA compute process is running. This is the authoritative signal that no map is
#      being built and no localization is in flight.
#   2. No established TCP connection to a published service port from off-box.
#   3. No non-loopback request in any service's access log within the window.
#   4. No backend job directory has been written recently.
#
# On (3): container healthchecks hit these services every 10 seconds and land in the same
# access logs, so loopback is filtered out — without that the instance would never look idle
# and would never shut down. A monitor must not create the activity it measures.
#
# The filter matches 127.0.0.1 ANYWHERE in the line, not just at the start, because the
# three services log in three different formats:
#
#   nginx (frontend)         127.0.0.1 - - [21/Aug/2026:05:38:37 +0000] "GET / HTTP/1.1" 200
#   uvicorn (maplocalizer)   INFO:     127.0.0.1:36072 - "GET / HTTP/1.1" 200 OK
#   Next.js (mapaligner)     (does not log requests at all)
#
# An anchored ^127\.0\.0\.1 filter catches nginx but not uvicorn, so maplocalizer's own
# healthcheck registers as user traffic and the host never shuts down. That bug was live
# until it was caught in testing.

set -uo pipefail

CONF=/opt/openvps/idle.conf
[ -r "$CONF" ] && . "$CONF"
IDLE_MINUTES="${IDLE_MINUTES:-30}"
MAPS_DIR="${MAPS_DIR:-/home/ubuntu/data/maps}"
STATE_DIR=/var/lib/openvps
STATE="$STATE_DIR/last-activity"
mkdir -p "$STATE_DIR"

log() { logger -t openvps-idle -- "$*"; echo "$(date -u +%FT%TZ) $*"; }

# 0 disables the timer's effect without needing to mask the unit.
if [ "$IDLE_MINUTES" -le 0 ]; then
  log "idle shutdown disabled (IDLE_MINUTES=$IDLE_MINUTES)"
  exit 0
fi

now=$(date +%s)
[ -f "$STATE" ] || echo "$now" > "$STATE"

reason=""

# 1. GPU compute processes -----------------------------------------------------------
gpu_procs="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c . || echo 0)"
[ "$gpu_procs" -gt 0 ] && reason="gpu:${gpu_procs}proc"

# 2. Established connections to the service ports, excluding loopback ----------------
if [ -z "$reason" ]; then
  conns=$(ss -Htn state established 2>/dev/null \
          | awk '{print $3, $4}' \
          | grep -E ':(80|3001|8000)\s' \
          | grep -vc '127\.0\.0\.1' || true)
  conns=${conns:-0}
  [ "$conns" -gt 0 ] && reason="conns:${conns}"
fi

# 3. Non-loopback HTTP activity in the access logs -----------------------------------
# `docker logs --since` is used rather than reading files, so this works regardless of
# where each image sends its log.
if [ -z "$reason" ]; then
  for c in openvps-frontend-1 openvps-mapaligner-1 openvps-maplocalizer-1; do
    docker inspect "$c" >/dev/null 2>&1 || continue
    hits=$(docker logs --since "${IDLE_MINUTES}m" "$c" 2>&1 \
           | grep -v '127\.0\.0\.1' \
           | grep -cE '"(GET|POST|PUT|PATCH|DELETE) ' || true)
    hits=${hits:-0}
    if [ "$hits" -gt 0 ]; then reason="http:${c}:${hits}"; break; fi
  done
fi

# 4. Recent writes under the maps directory ------------------------------------------
# Catches a long-running CPU-only stage (COLMAP bundle adjustment, zip extraction) that
# holds no CUDA context and serves no HTTP.
if [ -z "$reason" ] && [ -d "$MAPS_DIR" ]; then
  recent=$(find "$MAPS_DIR" -mmin "-${IDLE_MINUTES}" -type f -print -quit 2>/dev/null)
  [ -n "$recent" ] && reason="maps-write"
fi

if [ -n "$reason" ]; then
  echo "$now" > "$STATE"
  log "busy ($reason); resetting idle clock"
  exit 0
fi

last=$(cat "$STATE" 2>/dev/null || echo "$now")
idle_for=$(( (now - last) / 60 ))
if [ "$idle_for" -lt "$IDLE_MINUTES" ]; then
  log "idle ${idle_for}m of ${IDLE_MINUTES}m"
  exit 0
fi

log "idle ${idle_for}m >= ${IDLE_MINUTES}m; stopping compose and shutting down"
# Stop compose first so containers exit cleanly and any in-flight write to the maps volume
# is flushed before the filesystem goes away.
systemctl stop openvps.service || true
sync
shutdown -h now "openvps: idle for ${idle_for} minutes"
