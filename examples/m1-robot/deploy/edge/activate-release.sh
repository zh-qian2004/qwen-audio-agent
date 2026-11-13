#!/usr/bin/env bash
set -euo pipefail

base=/userdata/developer/audio-agent
name=${1:?release name required}
[[ $name =~ ^edge-[0-9]{8}-[0-9]{6}-[0-9a-f]{7}-[0-9a-f]{8}$ ]] || { echo 'invalid release name' >&2; exit 2; }
release="$base/releases/$name"
[[ -f $release/release-manifest.json && -d $release/node_modules ]] || { echo 'release not installed' >&2; exit 1; }
node="$base/runtime/node/bin/node"
"$node" "$release/verify-release.mjs" "$release"

active_file="$base/run/active-release"
old=$(<"$active_file")
[[ $old =~ ^[a-zA-Z0-9._-]+$ ]] || { echo 'invalid previous release' >&2; exit 1; }
if [[ $old == "$name" ]]; then
  echo "release already active: $name"
  exit 0
fi

bash "$base/scripts/edge-service.sh" stop
printf '%s\n' "$name" >"$active_file.next"
mv -f "$active_file.next" "$active_file"
if bash "$base/scripts/edge-service.sh" start; then
  echo "activated release: $name (previous: $old)"
  exit 0
fi

echo "activation failed, restoring $old" >&2
bash "$base/scripts/edge-service.sh" stop || true
printf '%s\n' "$old" >"$active_file.next"
mv -f "$active_file.next" "$active_file"
bash "$base/scripts/edge-service.sh" start
exit 1
