#!/usr/bin/env bash
# initialize.sh — runs on the HOST before the containers are created
# (devcontainer.json → initializeCommand).
#
# 1. Checks that a project layer is linked into the slot .devcontainer/project/
# 2. Resolves the enabled modules (project/modules, default: claude)
# 3. Checks the host prerequisites that Docker would otherwise silently "fix"
#    by creating directories (generic, per module, per project layer)
# 4. Builds the generic base image, then stacks the module images on top of it
# 5. Read-only protection for paths the host runs (protected-paths)
# 6. Generates .generated/:
#    compose.yml             module image as BASE_IMAGE, module mounts, read-only
#                            protection (.git/config + .git/hooks of every working
#                            repo, protected-paths, the project layer if it lives
#                            outside .devcontainer/), the project's Claude settings,
#                            the modules' compose files (include) and what their
#                            'configure' hooks ask for (networks, hosts, NO_PROXY)
#    <module>.env            per module with a 'configure' hook: values its
#                            compose.yml interpolates
#    protection-state        what was missing at this start, to warn about new paths
#    allowlist-modules.txt   proxy allowlist of the modules
#
# NO_CACHE=1 bash .devcontainer/initialize.sh  rebuilds all images without cache
# (pulls the latest agent CLIs).
# AI_ENVELOPE_IMAGE_PREFIX=<prefix> names the images <prefix>-base, <prefix>-mod-...
# instead of ai-envelope-... (used by tests/sandbox-test.sh, so it never
# replaces your images).

set -euo pipefail
cd "$(dirname "$0")"

IMAGE_PREFIX="${AI_ENVELOPE_IMAGE_PREFIX:-ai-envelope}"
BASE_IMAGE="${IMAGE_PREFIX}-base:latest"
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

# Module hooks (modules/<m>/configure): read the project's settings for the
# module, check them, and print what the devcontainer needs, one per line:
#   network <name> | extra_host <host> <ip> | no_proxy <host>
# A hook may write the values its compose.yml interpolates to $MODULE_ENV.
mkdir -p "$GEN"
DC_NETWORKS=() DC_EXTRA_HOSTS=() DC_NO_PROXY=()
for m in "${MODULES[@]}"; do
  [ -f "modules/$m/configure" ] || continue
  rm -f "$GEN/$m.env"
  out="$(MODULE_ENV="$GEN/$m.env" bash "modules/$m/configure")" || { echo "[init] ERROR: module '$m': configure failed (see above)."; exit 1; }
  while read -r kind a b; do
    case "$kind" in
      network)    DC_NETWORKS+=("$a") ;;
      extra_host) DC_EXTRA_HOSTS+=("$a:$b") ;;
      no_proxy)   DC_NO_PROXY+=("$a") ;;
      "")         ;;
      *)          echo "[init] ERROR: module '$m': configure printed an unknown line '$kind $a $b'."; exit 1 ;;
    esac
  done <<< "$out"
done

# ── 4. Images: base, then one layer per module ────────────────────────────────
echo "[init] Project layer: .devcontainer/project -> $(readlink -f project)"
echo "[init] Modules: ${MODULES[*]:-(none)}"
echo "[init] Building base image ${BASE_IMAGE} ..."
docker build "${BUILD_OPTS[@]}" "${BASE_OPTS[@]}" -t "$BASE_IMAGE" base

# Tag per chain (ai-envelope-mod-claude-copilot), so projects with different
# module sets don't overwrite each other's images. Modules without a Dockerfile
# (e.g. ntfy) only contribute mounts/allowlist.
image="$BASE_IMAGE"
chain="${IMAGE_PREFIX}-mod"
for m in "${MODULES[@]}"; do
  [ -f "modules/$m/Dockerfile" ] || continue
  chain="${chain}-${m}"
  # Named build contexts (modules/<m>/build-contexts: '<name> <host dir>'), for
  # files outside the module folder, e.g. the sandbox CA certificate
  CONTEXTS=()
  while read -r name dir; do
    dir="${dir//\$\{HOME\}/$HOME}"; dir="${dir/#\~/$HOME}"
    CONTEXTS+=(--build-context "${name}=${dir}")
  done < <(strip "modules/$m/build-contexts")
  echo "[init] Building module '$m' as ${chain}:latest ..."
  docker build "${BUILD_OPTS[@]}" "${CONTEXTS[@]}" -t "${chain}:latest" --build-arg "BASE_IMAGE=${image}" "modules/$m"
  image="${chain}:latest"
done

# ── 5. Read-only protection ───────────────────────────────────────────────────
# Paths that tools on the HOST run or load without asking (git hooks/config,
# Claude/VS Code/devcontainer settings, ...) are mounted read-only, in every
# working repo and in the envelope root: protected-paths (generic) adjusted by
# project/protected-paths. Plus .git/config + .git/hooks of every working repo
# and the project layer itself.
#
# protect <path> (relative to the workspace root, must exist) mounts the path
# read-only and every directory above it read-write onto itself. A mount point
# can't be renamed (EBUSY), its parent can: without those extra mounts the agent
# could 'mv repo/.git repo/.git-x' and build a new .git with a planted hook.
declare -A RW_DIRS=() RO_PATHS=()
NOT_PROTECTED=()   # protected paths that don't exist (yet): watched, see below

# true if <path> or one of its parent directories is a symlink. Never mount
# those: a planted '.vscode -> ~/.ssh' would mount the host's ~/.ssh.
has_symlink() {
  local dir="" part
  local -a parts
  IFS=/ read -ra parts <<< "$1"
  for part in "${parts[@]}"; do
    dir="${dir:+$dir/}$part"
    [ -L "../$dir" ] && return 0
  done
  return 1
}

protect() {
  local path="$1" dir="" part
  local -a parts=()
  if has_symlink "$path"; then
    echo "[init] WARNING: ${path} is or lies under a symlink — NOT protected (check where it points)."
    return
  fi
  [[ "$path" == */* ]] && IFS=/ read -ra parts <<< "${path%/*}"
  for part in "${parts[@]}"; do
    dir="${dir:+$dir/}$part"
    RW_DIRS[$dir]=1
  done
  RO_PATHS[$path]=1
}

# Protected paths per repo: generic list, then the project's additions and
# '!path' removals
mapfile -t PROTECTED < <(strip protected-paths)
while read -r line; do
  if [[ "$line" == '!'* ]]; then
    for i in "${!PROTECTED[@]}"; do
      [ "${PROTECTED[$i]}" = "${line#!}" ] && unset 'PROTECTED[i]'
    done
  else
    PROTECTED+=("$line")
  fi
done < <(strip project/protected-paths)
for p in "${PROTECTED[@]}"; do
  if [[ "$p" == /* || "/$p/" == */../* ]]; then
    echo "[init] ERROR: protected path '$p' must be relative to the repo, without '..'."
    exit 1
  fi
done

# protect_repo <repo> (relative to the workspace root, '' = envelope root)
protect_repo() {
  local repo="$1" p full done_list=()
  local -a paths=("${PROTECTED[@]}")
  if [ -n "$repo" ]; then
    paths+=(.git/config .git/hooks)
    # core.hooksPath inside the repo (e.g. .husky): git runs those instead of .git/hooks.
    # 'git config -f' only parses the file, it runs nothing from it.
    local hp
    hp="$(git config -f "../$repo/.git/config" --get core.hooksPath 2>/dev/null || true)"
    if [ -n "$hp" ]; then
      case "$hp" in
        /*|~*) echo "[init] WARNING: ${repo}: core.hooksPath ${hp} is outside the repo — not protected by the sandbox." ;;
        *)     paths+=("${hp%/}") ;;
      esac
    fi
  fi
  for p in "${paths[@]}"; do
    # The envelope's .devcontainer/ is read-only already (docker-compose.yml)
    [ -z "$repo" ] && [ "${p#./}" = .devcontainer ] && continue
    full="${repo:+$repo/}${p#./}"
    if [ -e "../$full" ] || [ -L "../$full" ]; then
      protect "$full"
      [[ -n "${RO_PATHS[$full]:-}" ]] && done_list+=("${p#./}")
    else
      NOT_PROTECTED+=("$full")
    fi
  done
  echo "[init] ${repo:-envelope root}: read-only ${done_list[*]:-(nothing)}"
}

# Working repos: every git repo up to two levels below the workspace root
# (except the envelope itself, whose .git is mounted read-only as a whole)
REPOS=()
while IFS= read -r gitdir; do
  repo="${gitdir%/.git}"; repo="${repo#../}"
  if [ ! -d "$gitdir" ] || [ -L "$gitdir" ]; then
    echo "[init] WARNING: ${repo}/.git is not a directory (worktree/submodule/symlink?) — its git config is NOT protected."
    continue
  fi
  mkdir -p "$gitdir/hooks"
  [ -f "$gitdir/config" ] || { echo "[init] WARNING: ${repo}/.git/config missing — skipped."; continue; }
  REPOS+=("$repo")
done < <(find .. -mindepth 2 -maxdepth 3 -name .git -not -path '../.devcontainer/*' | sort)

protect_repo ""
for repo in "${REPOS[@]}"; do
  protect_repo "$repo"
done

# Project layer outside .devcontainer/ (e.g. in a working repo): read-only too,
# so the agent can't change masks, allowlist, compose.yml or Dockerfile for the
# next rebuild
ws="$(readlink -f ..)"
layer="$(readlink -f project)"
case "$layer" in
  "$ws/.devcontainer"/*) ;;
  "$ws"/*) rel="${layer#"$ws"/}"
           protect "$rel"
           echo "[init] Project layer ${rel} mounted read-only" ;;
  *)       echo "[init] WARNING: project layer is outside the workspace; check-exclusions can't read it in the container." ;;
esac

# What can't be mounted is watched instead: a mount would make Docker create the
# path on the host (as root). Warn when a protected path or a repo shows up that
# wasn't there at the last start — the agent may have created it. From then on
# it is protected, but its content needs a look before host tools use it.
mkdir -p "$GEN"
STATE="$GEN/protection-state"
if [ -f "$STATE" ]; then
  while IFS=' ' read -r kind path; do
    case "$kind" in
      missing) [ -e "../$path" ] || [ -L "../$path" ] || continue
               echo "[init] WARNING: ${path} is new since the last start. Review it before tools on the host use it." ;;
      repo)    ;;
    esac
  done < "$STATE"
  for repo in "${REPOS[@]}"; do
    grep -qxF "repo $repo" "$STATE" ||
      echo "[init] WARNING: working repo ${repo} is new since the last start. If the agent created it, check its .git/config and .git/hooks before running git there."
  done
fi
{
  for p in "${NOT_PROTECTED[@]}"; do echo "missing $p"; done
  for repo in "${REPOS[@]}"; do echo "repo $repo"; done
} > "$STATE"

# Mount list: read-write parents, except inside a read-only path (that would
# make part of it writable again)
PROTECT_MOUNTS=()
for d in "${!RW_DIRS[@]}"; do
  inside_ro=0
  for r in "${!RO_PATHS[@]}"; do
    [[ "$d" == "$r" || "$d" == "$r"/* ]] && { inside_ro=1; break; }
  done
  [ "$inside_ro" -eq 1 ] || PROTECT_MOUNTS+=("../${d}:/workspace/${d}")
done
for r in "${!RO_PATHS[@]}"; do
  PROTECT_MOUNTS+=("../${r}:/workspace/${r}:ro")
done
if [ "${#PROTECT_MOUNTS[@]}" -gt 0 ]; then
  mapfile -t PROTECT_MOUNTS < <(printf '%s\n' "${PROTECT_MOUNTS[@]}" | sort)
fi

# ── 6. Generated files ────────────────────────────────────────────────────────

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
    PROTECT_MOUNTS+=("./project/claude-settings.json:/etc/claude-code/managed-settings.d/50-project.json:ro")
    echo "[init] Project Claude settings: claude-settings.json -> managed-settings.d/50-project.json"
  fi
fi

rm -f "$GEN/modules.yml"   # pre-rename leftover
# Volumes: module mounts (${HOME} is kept literally, compose interpolates it),
# then the read-only protection
VOLUMES=()
for m in "${MODULES[@]}"; do
  while IFS= read -r v; do VOLUMES+=("\"$v\"   # module $m"); done < <(strip "modules/$m/mounts")
done
for v in "${PROTECT_MOUNTS[@]}"; do
  case "$v" in
    *:ro) VOLUMES+=("\"$v\"   # read-only") ;;
    *)    VOLUMES+=("\"$v\"   # parent of a read-only path (can't be renamed)") ;;
  esac
done

{
  echo "# GENERATED by initialize.sh — do not edit."
  # Modules with their own services (e.g. cred-gw). Paths are relative to
  # .devcontainer/ (the project directory), not to this file.
  INCLUDES=()
  for m in "${MODULES[@]}"; do
    [ -f "modules/$m/compose.yml" ] && INCLUDES+=("$m")
  done
  if [ "${#INCLUDES[@]}" -gt 0 ]; then
    echo "include:"
    for m in "${INCLUDES[@]}"; do
      echo "  - path: ./modules/$m/compose.yml"
      [ -f "$GEN/$m.env" ] && echo "    env_file: ./${GEN#./}/$m.env"
    done
  fi
  echo "services:"
  echo "  devcontainer:"
  echo "    build:"
  echo "      args:"
  echo "        BASE_IMAGE: \"${image}\""
  if [ "${#VOLUMES[@]}" -gt 0 ]; then
    echo "    volumes:"
    printf '      - %s\n' "${VOLUMES[@]}"
  fi
  if [ "${#DC_NETWORKS[@]}" -gt 0 ]; then
    # Keeps 'sandbox' from docker-compose.yml: compose merges the network lists
    echo "    networks:"
    printf '      - %s\n' "${DC_NETWORKS[@]}"
  fi
  if [ "${#DC_EXTRA_HOSTS[@]}" -gt 0 ]; then
    echo "    extra_hosts:"
    printf '      - "%s"\n' "${DC_EXTRA_HOSTS[@]}"
  fi
  if [ "${#DC_NO_PROXY[@]}" -gt 0 ]; then
    # Replaces the value from docker-compose.yml: keep its entries first
    no_proxy="localhost,127.0.0.1$(printf ',%s' "${DC_NO_PROXY[@]}")"
    echo "    environment:"
    echo "      NO_PROXY: \"${no_proxy}\""
    echo "      no_proxy: \"${no_proxy}\""
  fi
} > "$GEN/compose.yml"

{
  echo "# GENERATED by initialize.sh from the enabled modules — do not edit."
  for m in "${MODULES[@]}"; do
    [ -f "modules/$m/allowlist.txt" ] || continue
    echo "# module $m"
    strip "modules/$m/allowlist.txt"
  done
} > "$GEN/allowlist-modules.txt"

echo "[init] Wrote ${GEN}/compose.yml and ${GEN}/allowlist-modules.txt"
