#!/bin/sh
# Only the uv installer and the selected package index are contacted. sherd itself sends no data.
set -eu

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) printf '%s\n' 'sherd supports macOS and Linux only.' >&2; exit 1 ;;
esac

if [ "${SHERD_DRY_RUN:-0}" = 1 ]; then
  if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'Install uv: download https://astral.sh/uv/install.sh with curl -fLsS, then run sh'
  fi
  printf 'uv tool install sherd-cli%s%s\n' "${SHERD_VERSION:+==${SHERD_VERSION}}" "${SHERD_FIND_LINKS:+ --find-links ${SHERD_FIND_LINKS}}"
  printf '%s\n' 'Next: sherd demo && sherd web --demo'
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv is missing; installing it with the official Astral installer.'
  installer=$(mktemp)
  if ! curl -fLsS https://astral.sh/uv/install.sh -o "$installer"; then
    rm -f "$installer"
    printf '%s\n' 'Failed to download the uv installer.' >&2
    exit 1
  fi
  if ! sh "$installer"; then
    rm -f "$installer"
    printf '%s\n' 'Failed to run the uv installer.' >&2
    exit 1
  fi
  rm -f "$installer"
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

set -- tool install "sherd-cli${SHERD_VERSION:+==${SHERD_VERSION}}"
if [ -n "${SHERD_FIND_LINKS:-}" ]; then
  set -- "$@" --find-links "$SHERD_FIND_LINKS"
fi
uv "$@"
if ! command -v sherd >/dev/null 2>&1; then
  printf '%s\n' 'If sherd is not on PATH, run: uv tool update-shell'
fi
printf '%s\n' 'Next: sherd demo && sherd web --demo'
