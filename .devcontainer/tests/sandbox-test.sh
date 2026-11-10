#!/usr/bin/env bash
# sandbox-test.sh — end-to-end test of the sandbox protections, on the host.
#
# Builds a throwaway workspace (a copy of this envelope + fake working repos and
# a project layer), runs initialize.sh, starts the devcontainer service via
# compose (project name 'sandbox-test') and checks from inside the container:
#   - secret masks hold and check-exclusions passes
#   - protected paths can't be changed, and can't be renamed away
#   - normal work (edit, move, commit, checkout, gc) still works
#   - on the next start, initialize.sh warns about new paths and symlinks
#
#   bash .devcontainer/tests/sandbox-test.sh
#   NO_BUILD=1 bash .devcontainer/tests/sandbox-test.sh   # use the existing
#       ai-envelope-* images instead of rebuilding (and retagging) them
#
# Needs: docker with compose v2, git. Run in CI by .github/workflows/ci.yml.

set -euo pipefail

ENVELOPE="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
WS="$TMP/ws"
export HOME="$TMP/home"
COMPOSE=(docker compose -p sandbox-test -f docker-compose.yml -f .generated/compose.yml -f project/compose.yml)
FAILED=0

cleanup() {
  (cd "$WS/.devcontainer" 2>/dev/null && "${COMPOSE[@]}" down --rmi local -v >/dev/null 2>&1) || true
  # The container created files as uid 1000; remove them as root in a container
  docker run --rm --network none -u 0 -v "$TMP:/t" --entrypoint sh ai-envelope-base:latest \
    -c 'rm -rf /t/* /t/.[!.]*' >/dev/null 2>&1 || true
  rm -rf "$TMP" 2>/dev/null || true
}
trap cleanup EXIT

fail() { echo "FAIL: $*"; FAILED=1; }

# ── Fixture ───────────────────────────────────────────────────────────────────
echo "== Fixture in $TMP"
mkdir -p "$WS" "$HOME/.claude-devcontainer"
echo '{}' > "$HOME/.claude-devcontainer.json"
touch "$HOME/.gitconfig"

# The envelope: tracked + new files only (no working repos, no .generated/, no slot)
git -C "$ENVELOPE" ls-files -z --cached --others --exclude-standard \
  | (cd "$ENVELOPE" && tar --null -T - -cf -) | tar -C "$WS" -xf -
mkdir -p "$WS/.git" "$WS/.claude"

# repo-a: host-executed config, a core.hooksPath and two secrets to mask
git init -q "$WS/repo-a"
echo '# repo-a' > "$WS/repo-a/README.md"
mkdir -p "$WS/repo-a/.claude" "$WS/repo-a/.vscode" "$WS/repo-a/.husky" "$WS/repo-a/configs"
echo '{}' > "$WS/repo-a/.claude/settings.json"
echo 'exit 0' > "$WS/repo-a/.husky/pre-commit"; chmod +x "$WS/repo-a/.husky/pre-commit"
git -C "$WS/repo-a" config core.hooksPath .husky
echo SECRET > "$WS/repo-a/.env"
echo SECRET > "$WS/repo-a/configs/secrets.yml"

# repo-b: holds the project layer, plus a planted symlink
git init -q "$WS/repo-b"
echo '# repo-b' > "$WS/repo-b/README.md"
ln -s /etc/passwd "$WS/repo-b/.envrc"
LAYER="$WS/repo-b/ai-envelope"
cp -r "$WS/.devcontainer/project-template" "$LAYER"
ln -s ../repo-b/ai-envelope "$WS/.devcontainer/project"
mkdir -p "$LAYER/dummies/repo-a/configs"
echo dummy > "$LAYER/dummies/repo-a/configs/secrets.yml"
printf 'repo-a/.env\nrepo-a/configs\n' > "$LAYER/excluded-files"
cat > "$LAYER/compose.yml" <<'EOF'
name: sandbox-test
services:
  devcontainer:
    volumes:
      - /dev/null:/workspace/repo-a/.env:ro
      - ./project/dummies/repo-a/configs:/workspace/repo-a/configs:ro
EOF

# CI runners don't run as uid 1000 ('dev' in the container): make the fixture
# writable for it. Read-only mounts stay read-only regardless.
chmod -R a+rwX "$WS"

# ── initialize.sh ─────────────────────────────────────────────────────────────
INIT_ENV=()
if [ "${NO_BUILD:-}" = 1 ]; then
  # Shim: skip 'docker build', pass everything else to the real docker
  mkdir -p "$TMP/bin"
  printf '#!/bin/sh\n[ "$1" = build ] && exit 0\nexec %s "$@"\n' "$(command -v docker)" > "$TMP/bin/docker"
  chmod +x "$TMP/bin/docker"
  INIT_ENV=(env "PATH=$TMP/bin:$PATH")
fi

echo "== initialize.sh"
"${INIT_ENV[@]}" bash "$WS/.devcontainer/initialize.sh" > "$TMP/init1.log" 2>&1 \
  || { cat "$TMP/init1.log"; echo "FAIL: initialize.sh"; exit 1; }
grep -q 'repo-b/.envrc is or lies under a symlink' "$TMP/init1.log" || fail "no warning for the symlinked .envrc"
grep -q 'repo-b/.envrc:/' "$WS/.devcontainer/.generated/compose.yml" && fail "symlinked .envrc was mounted"

cd "$WS/.devcontainer"
"${COMPOSE[@]}" config -q || { echo "FAIL: compose config"; exit 1; }
"${COMPOSE[@]}" build -q devcontainer > "$TMP/build.log" 2>&1 \
  || { cat "$TMP/build.log"; echo "FAIL: build"; exit 1; }

# ── Inside the container ──────────────────────────────────────────────────────
echo "== Container checks"
"${COMPOSE[@]}" run --rm --no-deps -T \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  devcontainer bash -s <<'EOF' || FAILED=1
cd /workspace
rc=0
expect() {   # expect blocked|allowed <label> <command...>
  local want="$1" label="$2" got; shift 2
  if "$@" >/dev/null 2>&1; then got=allowed; else got=blocked; fi
  if [ "$got" = "$want" ]; then printf '  ok    %-8s %s\n' "$got" "$label"
  else printf '  FAIL  %-8s %s (expected %s)\n' "$got" "$label" "$want"; rc=1; fi
}
G=(git -c user.email=t@example.com -c user.name=test)

echo "-- masks"
[ ! -s repo-a/.env ] && echo "  ok    .env is empty" || { echo "  FAIL  .env is readable"; rc=1; }
[ "$(cat repo-a/configs/secrets.yml)" = dummy ] && echo "  ok    configs/ is the dummy" || { echo "  FAIL  configs/ is not masked"; rc=1; }
expect allowed "check-exclusions passes" check-exclusions

echo "-- attacks"
expect blocked "rename repo-a/.git"              mv repo-a/.git repo-a/.git-x
expect blocked "rename repo-a"                   mv repo-a repo-a-x
expect blocked "rename repo-b (project layer)"   mv repo-b repo-b-x
expect blocked "rename repo-a/.claude"           mv repo-a/.claude repo-a/.claude-x
expect blocked "edit .claude/settings.json"      sh -c 'echo x >> repo-a/.claude/settings.json'
expect blocked "edit core.hooksPath hook"        sh -c 'echo evil >> repo-a/.husky/pre-commit'
expect blocked "set core.fsmonitor"              git -C repo-a config core.fsmonitor evil
expect blocked "add a .git/hooks hook"           touch repo-a/.git/hooks/post-checkout
expect blocked "edit project layer compose.yml"  sh -c 'echo x >> repo-b/ai-envelope/compose.yml'
expect blocked "write envelope .claude/"         touch .claude/settings.json
expect blocked "edit envelope .devcontainer/"    touch .devcontainer/x
# A /dev/null mask accepts writes (they go to /dev/null); the secret must stay hidden
expect allowed "write to masked .env goes nowhere" sh -c 'echo x > repo-a/.env && [ ! -s repo-a/.env ]'

echo "-- normal work"
expect allowed "edit a file"                     sh -c 'echo change >> repo-a/README.md'
expect allowed "new file + rename in repo"       sh -c 'echo n > repo-a/new.txt && mv repo-a/new.txt repo-a/new2.txt'
expect allowed "move a file across repos"        mv repo-a/new2.txt repo-b/moved.txt
expect allowed "git add + commit"                sh -c 'cd repo-a && "$@" add README.md && "$@" commit -qm test' _ "${G[@]}"
expect allowed "git checkout -b"                 git -C repo-a checkout -qb feature/x
expect allowed "git gc"                          git -C repo-a gc -q
expect allowed "create repo-b/.mcp.json"         sh -c 'echo {} > repo-b/.mcp.json'
exit $rc
EOF

[ "$(cat "$WS/repo-a/.env")" = SECRET ] || fail "the real repo-a/.env on the host was changed"

# ── Next start: warnings about what appeared ──────────────────────────────────
echo "== Next start"
"${INIT_ENV[@]}" bash initialize.sh > "$TMP/init2.log" 2>&1 \
  || { cat "$TMP/init2.log"; echo "FAIL: second initialize.sh"; exit 1; }
if grep -q 'repo-b/.mcp.json is new since the last start' "$TMP/init2.log"; then
  echo "  ok    warns about the new repo-b/.mcp.json"
else
  fail "no warning for the new repo-b/.mcp.json"
fi
grep -q '"../repo-b/.mcp.json:/workspace/repo-b/.mcp.json:ro"' .generated/compose.yml \
  && echo "  ok    repo-b/.mcp.json is read-only from now on" \
  || fail "repo-b/.mcp.json not protected after it appeared"

echo
if [ "$FAILED" -eq 0 ]; then echo "sandbox-test: all checks passed"; else echo "sandbox-test: FAILED"; exit 1; fi
