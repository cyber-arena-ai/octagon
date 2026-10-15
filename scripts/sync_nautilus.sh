#!/usr/bin/env bash
# Re-vendor nautilus-institute/finals-2025 :: ico into the challenge package.
#
# The ico source is now COMMITTED in-repo at
#   challenge-warehouse/YueyangPan/nautilus_ico/ico/
# so the challenge builds on a fresh host with no out-of-band clone. This script
# is only needed to UPGRADE the pin. To bump:
#   1. Update PINNED_SHA below.
#   2. Run this script (re-materializes ico/service + LICENSE/README/config).
#   3. Update `_VENDOR_PIN` in nautilus_ico/challenge.py to the new short SHA.
#   4. Re-run the full DEFCON test suite (incl. the live Claude smoke).
#   5. Single commit in challenge-warehouse with the new source + pin.
#
# Pinning (not tracking main) so builds are reproducible.

set -euo pipefail

REPO_URL="https://github.com/nautilus-institute/finals-2025.git"
PINNED_SHA="366520c0cc43c08c0eaf0a9aadd3d813df01ce88"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHAL_DIR="${ROOT}/challenge-warehouse/YueyangPan/nautilus_ico"
DEST="${CHAL_DIR}/ico"                       # committed vendored source
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

echo ">>> cloning ${REPO_URL} @ ${PINNED_SHA}"
git clone --quiet "${REPO_URL}" "${TMP}/finals-2025"
git -C "${TMP}/finals-2025" checkout --quiet "${PINNED_SHA}"

SRC="${TMP}/finals-2025/ico"
[ -d "${SRC}/service" ] || { echo "!! ${SRC}/service missing at pin" >&2; exit 1; }

echo ">>> re-vendoring ico/service + LICENSE/README/config → ${DEST}"
rm -rf "${DEST}/service"
mkdir -p "${DEST}"
cp -R "${SRC}/service" "${DEST}/service"
for f in LICENSE README.md challenge_config.yaml; do
    [ -f "${SRC}/${f}" ] && cp "${SRC}/${f}" "${DEST}/${f}"
done
# drop any build artifacts / vcs that slipped in
find "${DEST}" -name .git -prune -exec rm -rf {} + 2>/dev/null || true
find "${DEST}" \( -name '*.o' -o -name '*.ppu' \) -delete 2>/dev/null || true

SHORT="${PINNED_SHA:0:12}"
echo ">>> done. Now set _VENDOR_PIN = \"${SHORT}\" in nautilus_ico/challenge.py"
echo "    and commit ${DEST} + challenge.py together (see ico/VENDOR.md)."
