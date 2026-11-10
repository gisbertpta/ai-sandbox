#!/usr/bin/env bash
# initialize.sh — runs on the HOST before the containers are created
# (devcontainer.json → initializeCommand).
#
# 1. Checks that a project layer is linked into the slot .devcontainer/project/
# 2. Resolves the enabled modules (project/modules, default: claude)
# 3. Checks the host prerequisites that Docker would otherwise silently "fix"
#    by creating directories (generic, per module, per project layer)
# 4. Builds the generic base image, then stacks the module images on top of it
# 5. Generates .generated/:
#    compose.yml             module image as BASE_IMAGE, module mounts, read-only
#                            .git/config + .git/hooks of every working repo and the
#                            project layer (if it lives outside .devcontainer/),
#                            the project's Claude settings (project/claude-settings.json)
#    allowlist-modules.txt   proxy allowlist of the modules
#
# NO_CACHE=1 bash .devcontainer/initialize.sh  rebuilds all images without cache
# (pulls the latest agent CLIs).

set -euo pipefail
cd "$(dirname "$0")"

BASE_IMAGE="ai-envelope-base:latest"
DEFAULT_MODULES=(claude)
GEN=.generated
FAIL=0
BUILD_OPTS=()
BASE_OPTS=()
if [ "${NO_CACHE:-}" = 1 ]; then
  BUILD_OPTS=(--no-cache)
  BASE_OPTS=(--pull)   # only for the base: the module builds start FROM local images
fi

# Prints a data file without comments and blank lines
strip() {
  [ -f "$1" ] || return 0
  sed -e 's/[[:space:]]*#.*$//' -e '/^[[:space:]]*$/d' "$1"
}

# Checks a prereqs file: lines '<file|dir> <path> <hint>'. ${HOME} and a leading
# ~ are expanded; relative paths are relative to the workspace root.
check_prereqs() {
  local file="$1" owner="$2" kind path hint what
  while read -r kind path hint; do
    path="${path//\$\{HOME\}/$HOME}"
    path="${path/#\~/$HOME}"
    [[ "$path" == /* ]] || path="../$path"
    case "$kind" in
      file) [ -f "$path" ] && continue; what="a file" ;;
      dir)  [ -d "$path" ] && continue; what="a directory" ;;
      *)    echo "[init] ERROR: $file: unknown kind '$kind'."; FAIL=1; continue ;;
    esac
    echo "[init] ERROR: ${path#../} must exist as $what ($owner: $hint)."
    FAIL=1
  done < <(strip "$file")
}

# ── 1. Project layer ──────────────────────────────────────────────────────────
if [ ! -e project ]; then
  if [ -L project ]; then
    echo "[init] ERROR: .devcontainer/project is a broken symlink (-> $(readlink project))."
  else
    echo "[init] ERROR: no project layer at .devcontainer/project."
  fi
  echo "       Link or copy one, e.g.:"
  echo "         ln -s ../<repo>/<layer-dir> .devcontainer/project"
  echo "         cp -r .devcontainer/project-template <somewhere> && ln -s ... .devcontainer/project"
  echo "       See README.md (\"Starting a new project\")."
  exit 1
fi

for f in compose.yml Dockerfile Dockerfile.dockerignore excluded-files allowlist.txt; do
  if [ ! -f "project/$f" ]; then
    echo "[init] ERROR: project layer is missing '$f' (compare with .devcontainer/project-template/)."
    FAIL=1
  fi
done

# ── 2. Modules ────────────────────────────────────────────────────────────────
if [ -f project/modules ]; then
  mapfile -t MODULES < <(strip project/modules)
else
  MODULES=("${DEFAULT_MODULES[@]}")
fi

for m in "${MODULES[@]}"; do
  if [[ ! "$m" =~ ^[a-z0-9-]+$ ]] || [ ! -d "modules/$m" ]; then
    echo "[init] ERROR: unknown module '$m' in project/modules (available: $(cd modules && ls -d */ | tr -d / | tr '\n' ' '))."
    FAIL=1
  fi
done

# ── 3. Host prerequisites ─────────────────────────────────────────────────────
# A missing bind-mount source file would be created by Docker as a DIRECTORY.
if [ ! -f "$HOME/.gitconfig" ]; then
  echo "[init] ERROR: $HOME/.gitconfig must exist as a file."
  FAIL=1
fi
for m in "${MODULES[@]}"; do
  check_prereqs "modules/$m/prereqs" "module '$m'"
done
check_prereqs project/prereqs "project layer"

[ "$FAIL" -eq 0 ] || exit 1

# ── 4. Images: base, then one layer per module ────────────────────────────────
echo "[init] Project layer: .devcontainer/project -> $(readlink -f project)"
echo "[init] Modules: ${MODULES[*]:-(none)}"
echo "[init] Building base image ${BASE_IMAGE} ..."
docker build "${BUILD_OPTS[@]}" "${BASE_OPTS[@]}" -t "$BASE_IMAGE" base

# Tag per chain (ai-envelope-mod-claude-copilot), so projects with different
# module sets don't overwrite each other's images. Modules without a Dockerfile
# (e.g. ntfy) only contribute mounts/allowlist.
image="$BASE_IMAGE"
chain="ai-envelope-mod"
for m in "${MODULES[@]}"; do
  [ -f "modules/$m/Dockerfile" ] || continue
  chain="${chain}-${m}"
  echo "[init] Building module '$m' as ${chain}:latest ..."
  docker build "${BUILD_OPTS[@]}" -t "${chain}:latest" --build-arg "BASE_IMAGE=${image}" "modules/$m"
  image="${chain}:latest"
done

# ── 5. Generated files ────────────────────────────────────────────────────────
# Working repos: every git repo up to two levels below the workspace root
# (except the envelope itself, whose .git is mounted read-only as a whole).
# Their .git/config and .git/hooks become read-only, so the agent can't plant
# hooks, core.fsmonitor, aliases or filters that run when you use git on the host.
REPO_MOUNTS=()
while IFS= read -r gitdir; do
  repo="${gitdir%/.git}"; repo="${repo#../}"
  if [ ! -d "$gitdir" ]; then
    echo "[init] WARNING: ${repo}/.git is not a directory (worktree/submodule?) — its git config is NOT protected."
    continue
  fi
  mkdir -p "$gitdir/hooks"
  [ -f "$gitdir/config" ] || { echo "[init] WARNING: ${repo}/.git/config missing — skipped."; continue; }
  REPO_MOUNTS+=("../${repo}/.git/config:/workspace/${repo}/.git/config:ro"
                "../${repo}/.git/hooks:/workspace/${repo}/.git/hooks:ro")
  echo "[init] Working repo: ${repo} (.git/config + .git/hooks read-only)"
done < <(find .. -mindepth 2 -maxdepth 3 -name .git -not -path '../.devcontainer/*' | sort)

# Project layer outside .devcontainer/ (e.g. in a working repo): read-only too,
# so the agent can't change masks, allowlist or Dockerfile for the next rebuild
ws="$(readlink -f ..)"
layer="$(readlink -f project)"
case "$layer" in
  "$ws/.devcontainer"/*) ;;
  "$ws"/*) rel="${layer#"$ws"/}"
           REPO_MOUNTS+=("../${rel}:/workspace/${rel}:ro")
           echo "[init] Project layer ${rel} mounted read-only" ;;
  *)       echo "[init] WARNING: project layer is outside the workspace; check-exclusions can't read it in the container." ;;
esac

# Project Claude settings: merged over the module's managed settings as a drop-in
# (lists such as permissions.deny and hooks are combined). Read-only, so the agent
# can't change them. Claude refuses to start on invalid JSON, so check it here,
# with the jq of the image just built (the host may not have jq).
if [ -f project/claude-settings.json ]; then
  if [[ " ${MODULES[*]} " != *" claude "* ]]; then
    echo "[init] WARNING: project/claude-settings.json is ignored: module 'claude' is not enabled."
  elif ! docker run --rm -i --network none "$image" jq -e 'type == "object"' \
         < project/claude-settings.json > /dev/null; then
    echo "[init] ERROR: project/claude-settings.json must be a JSON object (strict JSON, no comments)."
    exit 1
  else
    REPO_MOUNTS+=("./project/claude-settings.json:/etc/claude-code/managed-settings.d/50-project.json:ro")
    echo "[init] Project Claude settings: claude-settings.json -> managed-settings.d/50-project.json"
  fi
fi

mkdir -p "$GEN"
rm -f "$GEN/modules.yml"   # pre-rename leftover
{
  echo "# GENERATED by initialize.sh — do not edit."
  echo "services:"
  echo "  devcontainer:"
  echo "    build:"
  echo "      args:"
  echo "        BASE_IMAGE: \"${image}\""
  echo "    volumes:"
  for m in "${MODULES[@]}"; do
    # ${HOME} is kept literally here, compose interpolates it
    strip "modules/$m/mounts" | sed -e "s|.*|      - \"&\"   # module $m|"
  done
  for v in "${REPO_MOUNTS[@]}"; do
    echo "      - \"$v\"   # read-only"
  done
} > "$GEN/compose.yml"
# An empty 'volumes:' is invalid compose; drop it when nothing adds mounts
grep -q '^      - ' "$GEN/compose.yml" || sed -i '/^    volumes:$/d' "$GEN/compose.yml"

{
  echo "# GENERATED by initialize.sh from the enabled modules — do not edit."
  for m in "${MODULES[@]}"; do
    [ -f "modules/$m/allowlist.txt" ] || continue
    echo "# module $m"
    strip "modules/$m/allowlist.txt"
  done
} > "$GEN/allowlist-modules.txt"

echo "[init] Wrote ${GEN}/compose.yml and ${GEN}/allowlist-modules.txt"
