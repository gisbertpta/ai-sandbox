#!/usr/bin/env bash
# compose.sh — docker compose with this repo's compose files (base + modules + project layer).
# Use it instead of a bare 'docker compose' so all files are always merged:
#   .devcontainer/compose.sh ps
#   .devcontainer/compose.sh logs -f proxy
#   .devcontainer/compose.sh restart proxy
dir="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$dir/.generated/compose.yml" ]; then
  echo "compose.sh: $dir/.generated/compose.yml missing — run initialize.sh first." >&2
  exit 1
fi
exec docker compose -f "$dir/docker-compose.yml" -f "$dir/.generated/compose.yml" \
                    -f "$dir/project/compose.yml" "$@"
