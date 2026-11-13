#!/usr/bin/env bash
set -euo pipefail

base=/userdata/developer/audio-agent
name=${1:?release name required}
expected_sha=${2:?archive SHA-256 required}
[[ $name =~ ^edge-[0-9]{8}-[0-9]{6}-[0-9a-f]{7}-[0-9a-f]{8}$ ]] || { echo 'invalid release name' >&2; exit 2; }
[[ $expected_sha =~ ^[0-9A-Fa-f]{64}$ ]] || { echo 'invalid archive checksum' >&2; exit 2; }

archive="$base/run/uploads/$name.tar.gz"
release="$base/releases/$name"
staging="$base/releases/.staging-$name"
node="$base/runtime/node/bin/node"
[[ -f $archive ]] || { echo 'release archive missing' >&2; exit 1; }
actual_sha=$(sha256sum "$archive")
actual_sha=${actual_sha%% *}
[[ ${actual_sha,,} == ${expected_sha,,} ]] || { echo 'release archive checksum mismatch' >&2; exit 1; }
[[ ! -e $release && ! -e $staging ]] || { echo 'release already exists' >&2; exit 1; }

mkdir -m 700 "$staging"
tar -xzf "$archive" -C "$staging"
"$node" "$staging/verify-release.mjs" "$staging"

if [[ -f $base/run/active-release ]]; then
  active=$(<"$base/run/active-release")
else
  active=candidate-20261112
fi
[[ $active =~ ^[a-zA-Z0-9._-]+$ ]] || { echo 'invalid active release' >&2; exit 1; }
if [[ -d $base/releases/$active/node_modules ]] && cmp -s \
  "$staging/package-lock.json" "$base/releases/$active/package-lock.json"; then
  cp -a "$base/releases/$active/node_modules" "$staging/node_modules"
else
  (cd "$staging" && PATH="$base/runtime/node/bin:$PATH" \
    npm_config_cache="$base/data/npm-cache" npm ci --omit=dev --ignore-scripts --no-audit --no-fund)
fi
"$node" --check "$staging/examples/m1-robot/start.mjs"
"$node" --check "$staging/examples/m1-robot/main.mjs"
"$node" --check "$staging/server/src/index.mjs"
"$node" "$staging/verify-release.mjs" "$staging"
mv "$staging" "$release"
echo "installed release: $name"
