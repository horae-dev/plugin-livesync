#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Smoke test of a built sidecar image, before it is pushed (CI job `publish`):
#
#   scripts/smoke-image.sh <image>
#
# No network and no CouchDB (the Integration job covered that for this commit): the image's
# user, entrypoint and healthcheck, the licence files, and `horae-livesync check` run as in
# production (read-only root, tmpfs /tmp, no capabilities) on a synthetic livesync.yaml whose
# two secrets come from environment variables (the Coolify layout). `check` resolves both
# secrets and asks the pinned LiveSync CLI for its default settings, so the CLI runs too.
set -euo pipefail

image="${1:?usage: smoke-image.sh <image>}"

config="$(docker image inspect "${image}" --format '{{ json .Config }}')"
jq -e '.User == "10001:10001"' <<< "${config}" > /dev/null || {
  echo "the image does not run as 10001:10001" >&2
  exit 1
}
jq -e '.Entrypoint == ["horae-livesync"] and .Cmd == ["run"]' <<< "${config}" > /dev/null || {
  echo "unexpected entrypoint or command" >&2
  exit 1
}
jq -e '.Healthcheck.Test == ["CMD", "horae-livesync", "health"]' <<< "${config}" > /dev/null || {
  echo "the image has no horae-livesync health HEALTHCHECK" >&2
  exit 1
}

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
cat > "${work}/livesync.yaml" <<'YAML'
schema_version: 1
couchdb:
  url: http://couchdb:5984
  database: obsidian
  username: horae
  password_secret: LIVESYNC_COUCHDB_PASSWORD
e2ee:
  passphrase_secret: LIVESYNC_E2EE_PASSPHRASE
secrets:
  refs:
    LIVESYNC_COUCHDB_PASSWORD: { env: LIVESYNC_COUCHDB_PASSWORD }
    LIVESYNC_E2EE_PASSPHRASE: { env: LIVESYNC_E2EE_PASSPHRASE }
YAML
chmod 0755 "${work}"
chmod 0644 "${work}/livesync.yaml"

run=(docker run --rm --read-only --tmpfs "/tmp:size=64m,noexec,nosuid,nodev"
  --cap-drop ALL --security-opt no-new-privileges --network none
  -v "${work}:/config:ro")

out="$("${run[@]}" \
  -e LIVESYNC_COUCHDB_PASSWORD=synthetic-smoke-password \
  -e LIVESYNC_E2EE_PASSPHRASE=synthetic-smoke-passphrase \
  "${image}" check)"
if [ "${out}" != "livesync.yaml and its secrets are valid" ]; then
  echo "check: unexpected output: ${out}" >&2
  exit 1
fi

# Without the variables, `check` must refuse (exit 78), naming the variable, not a value.
set +e
err="$("${run[@]}" "${image}" check 2>&1)"
code=$?
set -e
if [ "${code}" -ne 78 ] || [[ "${err}" != *"environment variable LIVESYNC_COUCHDB_PASSWORD is not set"* ]]; then
  echo "check without secrets: exit ${code}: ${err}" >&2
  exit 1
fi

"${run[@]}" --entrypoint sh "${image}" -c \
  'test -s /usr/share/licenses/horae-livesync/THIRD_PARTY_NOTICES &&
   test -s /usr/share/licenses/obsidian-livesync/LICENSE &&
   test -s /usr/share/licenses/node/LICENSE'

echo "${image}: smoke test passed"
