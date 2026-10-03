#!/bin/sh
# Only the uv installer and the selected package index are contacted. sherd itself sends no data.
set -eu

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) printf '%s\n' 'sherd supports macOS and Linux only.' >&2; exit 1 ;;
esac

if [ "${SHERD_DRY_RUN:-0}" = 1 ]; then
  if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh'
  fi
  printf 'uv tool install sherd-cli%s%s\n' "${SHERD_VERSION:+==${SHERD_VERSION}}" "${SHERD_FIND_LINKS:+ --find-links ${SHERD_FIND_LINKS}}"
  printf '%s\n' 'Next: sherd demo && sherd web'
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv is missing; installing it with the official Astral installer.'
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

set -- tool install "sherd-cli${SHERD_VERSION:+==${SHERD_VERSION}}"
if [ -n "${SHERD_FIND_LINKS:-}" ]; then
  set -- "$@" --find-links "$SHERD_FIND_LINKS"
fi
uv "$@"
printf '%s\n' 'Next: sherd demo && sherd web'
