#!/usr/bin/env bash
# Install the api-contract toolchain into one directory, every piece pinned:
#
#   oasdiff  - an exact release, downloaded and verified against the SHA-256 recorded below.
#   Spectral - and the YAML parser, from toolchain/package-lock.json with `npm ci` (each package's
#              integrity hash is in the lockfile) and `--ignore-scripts`.
#
# Usage: install-toolchain.sh <dest-dir>
#   Writes <dest-dir>/bin/oasdiff and <dest-dir>/node_modules/. Re-running reuses a matching
#   install. Never install into a shared path (/usr/local, a fixed /tmp): on a shared runner two
#   jobs would race on the same files. CI passes a directory under $RUNNER_TEMP.
set -euo pipefail

OASDIFF_VERSION="1.19.1"
# From https://github.com/oasdiff/oasdiff/releases/download/v1.19.1/checksums.txt

dest="${1:?usage: install-toolchain.sh <dest-dir>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${dest}/bin"
dest="$(cd "${dest}" && pwd)"

# (A case, not an associative array: macOS ships bash 3.2, which has none.)
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) platform="linux_amd64"; pinned="a20c2163c795d63fabd7f9640a945d9b1e7cb320062d4f24ea4d112d55f637c5" ;;
  Linux/aarch64 | Linux/arm64) platform="linux_arm64"; pinned="1175b64983dfa7fa41e8ffe307a705c59c009c0fca673ad6ebe91011247780c6" ;;
  Darwin/*) platform="darwin_all"; pinned="9f8a47e2d6dfde113a3358821b48314af7aa95f09d5581cd6659f95c979f3f62" ;;
  *) echo "install-toolchain: no pinned oasdiff build for $(uname -s)/$(uname -m)" >&2; exit 1 ;;
esac

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

if [ -x "${dest}/bin/oasdiff" ] && [ "$("${dest}/bin/oasdiff" --version 2>/dev/null)" = "oasdiff version ${OASDIFF_VERSION}" ]; then
  echo "oasdiff ${OASDIFF_VERSION} already installed in ${dest}/bin"
else
  work="$(mktemp -d "${dest}/.oasdiff.XXXXXX")"
  archive="oasdiff_${OASDIFF_VERSION}_${platform}.tar.gz"
  curl -fsSL --retry 3 -o "${work}/${archive}" \
    "https://github.com/oasdiff/oasdiff/releases/download/v${OASDIFF_VERSION}/${archive}"
  actual="$(sha256_of "${work}/${archive}")"
  if [ "${actual}" != "${pinned}" ]; then
    echo "install-toolchain: ${archive} checksum mismatch (got ${actual}, pinned ${pinned})" >&2
    rm -rf "${work}"
    exit 1
  fi
  tar -xzf "${work}/${archive}" -C "${work}" oasdiff
  mv "${work}/oasdiff" "${dest}/bin/oasdiff"
  chmod 0755 "${dest}/bin/oasdiff"
  rm -rf "${work}"
fi
"${dest}/bin/oasdiff" --version

# npm ci needs package.json beside the lockfile; copy both so node_modules lands in <dest>, not in
# the action's checkout (which other jobs on the same runner may share).
if ! cmp -s "${here}/toolchain/package-lock.json" "${dest}/package-lock.json" || [ ! -d "${dest}/node_modules" ]; then
  cp "${here}/toolchain/package.json" "${here}/toolchain/package-lock.json" "${dest}/"
  (cd "${dest}" && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error)
fi
"${dest}/node_modules/.bin/spectral" --version
