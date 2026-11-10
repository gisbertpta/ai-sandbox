#!/usr/bin/env bash
# sandbox-test.sh — end-to-end test of the sandbox protections, on the host.
#
# Builds a throwaway workspace (a copy of this repo + fake working repos and
# a project layer), runs initialize.sh, starts the devcontainer service via
# compose (project name 'sandbox-test') and checks from inside the container:
#   - secret masks hold and check-exclusions passes
#   - protected paths can't be changed, and can't be renamed away
#   - normal work (edit, move, commit, checkout, gc) still works
#   - on the next start, initialize.sh warns about new paths and symlinks
#   - module build secrets: the build sees the secret, the image doesn't hold it
#   - module cred-gw: upstream host names reach the gateway (not the proxy),
#     TLS verifies against the sandbox CA, the policy applies, the tokens stay
#     out of the devcontainer, and a missing token stops the start
#
#   bash .devcontainer/tests/sandbox-test.sh
#   NO_BUILD=1 bash .devcontainer/tests/sandbox-test.sh   # reuse your existing
#       ai-sandbox-base and ai-sandbox-mod-claude images (faster, but tests
#       the current scripts against the images you have)
#
# The test builds its images as ai-sandbox-test-* and removes them afterwards,
# so it never replaces your ai-sandbox-* images (the cred-gw module image of the
# test trusts a throwaway CA).
#
# Needs: docker with compose >= 2.20 (include), git, openssl. Run in CI by .github/workflows/ci.yml.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
WS="$TMP/ws"
export HOME="$TMP/home"
export AI_SANDBOX_IMAGE_PREFIX=ai-sandbox-test
COMPOSE=(docker compose -p sandbox-test -f docker-compose.yml -f .generated/compose.yml -f project/compose.yml)
FAILED=0

cleanup() {
  (cd "$WS/.devcontainer" 2>/dev/null && "${COMPOSE[@]}" down --rmi local -v >/dev/null 2>&1) || true
  # The container created files as uid 1000; remove them as root in a container
  docker run --rm --network none -u 0 -v "$TMP:/t" --entrypoint sh ai-sandbox-test-base:latest \
    -c 'rm -rf /t/* /t/.[!.]*' >/dev/null 2>&1 || true
  rm -rf "$TMP" 2>/dev/null || true
  docker images --format '{{.Repository}}:{{.Tag}}' 'ai-sandbox-test-*' | xargs -r docker rmi -f >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { echo "FAIL: $*"; FAILED=1; }

# ── Fixture ───────────────────────────────────────────────────────────────────
echo "== Fixture in $TMP"
mkdir -p "$WS" "$HOME/.claude-devcontainer"
echo '{}' > "$HOME/.claude-devcontainer.json"
touch "$HOME/.gitconfig"

# This repo: tracked + new files only (no working repos, no .generated/, no slot)
git -C "$REPO_ROOT" ls-files -z --cached --others --exclude-standard \
  | (cd "$REPO_ROOT" && tar --null -T - -cf -) | tar -C "$WS" -xf -
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
LAYER="$WS/repo-b/ai-sandbox"
cp -r "$WS/.devcontainer/project-template" "$LAYER"
ln -s ../repo-b/ai-sandbox "$WS/.devcontainer/project"
mkdir -p "$LAYER/dummies/repo-a/configs"
echo dummy > "$LAYER/dummies/repo-a/configs/secrets.yml"
printf 'repo-a/.env\nrepo-a/configs\n' > "$LAYER/excluded-files"
printf 'claude\ncred-gw\nsecret-probe\n' > "$LAYER/modules"

# secret-probe: a test-only module that reads a build secret and keeps only its hash
PROBE="$WS/.devcontainer/modules/secret-probe"
mkdir -p "$PROBE"
PROBE_SECRET="probe-secret-$RANDOM-$RANDOM-4711"
echo "$PROBE_SECRET" > "$HOME/probe-secret"
echo 'probe ${HOME}/probe-secret' > "$PROBE/build-secrets"
cat > "$PROBE/Dockerfile" <<'EOF'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN --mount=type=secret,id=probe,required=true sha256sum /run/secrets/probe | cut -d' ' -f1 > /opt/probe-sha
USER dev
EOF
cat > "$LAYER/cred-gw.env" <<'EOF'
JIRA_GW_UPSTREAM=https://jira.example.test
JIRA_GW_LABEL=sandbox-test
GITLAB_GW_UPSTREAM=https://gitlab.example.test
GITLAB_GW_READ=grp/repo-a
CRED_GW_SUBNET=172.30.98.0/28
EOF
cat > "$LAYER/compose.yml" <<'EOF'
name: sandbox-test
services:
  devcontainer:
    volumes:
      - /dev/null:/workspace/repo-a/.env:ro
      - ./project/dummies/repo-a/configs:/workspace/repo-a/configs:ro
EOF

# cred-gw: fake tokens, sandbox CA + gateway certificate for the two hosts
mkdir -p "$HOME/.config/ai-sandbox/cred-gw"
echo jira-fake-token-4711 > "$HOME/.config/ai-sandbox/cred-gw/jira-token"
echo gitlab-fake-token-4711 > "$HOME/.config/ai-sandbox/cred-gw/gitlab-token"
bash "$WS/.devcontainer/modules/cred-gw/make-certs.sh" jira.example.test gitlab.example.test > "$TMP/certs.log" 2>&1 \
  || { cat "$TMP/certs.log"; echo "FAIL: make-certs.sh"; exit 1; }

# CI runners don't run as uid 1000 ('dev' in the container, 'gw' in cred-gw):
# make the fixture readable/writable for it. Read-only mounts stay read-only.
chmod -R a+rwX "$WS"
chmod a+r "$HOME/.config/ai-sandbox/cred-gw"/* "$HOME/.config/ai-sandbox/sandbox-ca/cred-gw/tls.key"
chmod a+rx "$HOME/.config/ai-sandbox/sandbox-ca/cred-gw"

# ── initialize.sh ─────────────────────────────────────────────────────────────
INIT_ENV=()
if [ "${NO_BUILD:-}" = 1 ]; then
  # Your images under the test names; the shim then skips 'docker build -t <tag>'
  # when <tag> exists, so only the cred-gw module image is built
  for img in base mod-claude; do
    docker image inspect "ai-sandbox-$img:latest" > /dev/null 2>&1 \
      || { echo "FAIL: NO_BUILD=1 needs ai-sandbox-$img:latest (run once without it)"; exit 1; }
    docker tag "ai-sandbox-$img:latest" "ai-sandbox-test-$img:latest"
  done
  mkdir -p "$TMP/bin"
  cat > "$TMP/bin/docker" <<EOF
#!/usr/bin/env bash
if [ "\$1" = build ]; then
  prev=""
  for a in "\$@"; do
    if [ "\$prev" = -t ] && $(command -v docker) image inspect "\$a" > /dev/null 2>&1; then exit 0; fi
    prev="\$a"
  done
fi
exec $(command -v docker) "\$@"
EOF
  chmod +x "$TMP/bin/docker"
  INIT_ENV=(env "PATH=$TMP/bin:$PATH")
fi

echo "== initialize.sh"
"${INIT_ENV[@]}" bash "$WS/.devcontainer/initialize.sh" > "$TMP/init1.log" 2>&1 \
  || { cat "$TMP/init1.log"; echo "FAIL: initialize.sh"; exit 1; }
grep -q 'repo-b/.envrc is or lies under a symlink' "$TMP/init1.log" || fail "no warning for the symlinked .envrc"
grep -q 'repo-b/.envrc:/' "$WS/.devcontainer/.generated/compose.yml" && fail "symlinked .envrc was mounted"
grep -q 'Gateway 172.30.98.10 for jira.example.test gitlab.example.test' "$TMP/init1.log" || fail "cred-gw configure did not run"

cd "$WS/.devcontainer"
"${COMPOSE[@]}" config -q || { echo "FAIL: compose config"; exit 1; }
"${COMPOSE[@]}" build -q devcontainer cred-gw > "$TMP/build.log" 2>&1 \
  || { cat "$TMP/build.log"; echo "FAIL: build"; exit 1; }
"${COMPOSE[@]}" up -d cred-gw > "$TMP/up.log" 2>&1 || { cat "$TMP/up.log"; echo "FAIL: start cred-gw"; exit 1; }

echo "== Build secrets"
PROBE_IMAGE=ai-sandbox-test-mod-claude-cred-gw-secret-probe:latest
want="$(printf '%s\n' "$PROBE_SECRET" | sha256sum | cut -d' ' -f1)"
got="$(docker run --rm --network none --entrypoint cat "$PROBE_IMAGE" /opt/probe-sha 2>/dev/null || true)"
[ "$got" = "$want" ] && echo "  ok    the module build saw the secret" || fail "the module build did not get the secret (got '$got')"
if docker save "$PROBE_IMAGE" | grep -qaF "$PROBE_SECRET"; then
  fail "the secret is in the module image"
else
  echo "  ok    the secret is not in the image layers"
fi

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
expect blocked "edit project layer compose.yml"  sh -c 'echo x >> repo-b/ai-sandbox/compose.yml'
expect blocked "write workspace-root .claude/"   touch .claude/settings.json
expect blocked "edit /workspace/.devcontainer/"  touch .devcontainer/x
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

echo "-- cred-gw"
expect allowed "jira host resolves to the gateway" sh -c '[ "$(getent hosts jira.example.test | cut -d" " -f1)" = 172.30.98.10 ]'
expect allowed "jira host bypasses the proxy"    sh -c 'printf %s "$NO_PROXY" | grep -q jira.example.test'
# No -k: TLS must verify against the sandbox CA. A DELETE is never allowed,
# so the gateway answers 403 itself, without asking the upstream.
code=$(curl -sS -o /tmp/r -w '%{http_code}' -X DELETE https://jira.example.test/rest/api/2/issue/X-1 2>/tmp/e || true)
if [ "$code" = 403 ] && grep -q 'cred-gw:' /tmp/r; then echo "  ok    TLS verifies, policy denies DELETE (403 from cred-gw)"
else echo "  FAIL  expected a 403 from cred-gw, got '$code': $(cat /tmp/r /tmp/e 2>/dev/null | head -c 300)"; rc=1; fi
expect blocked "tokens not mounted here"         ls /run/cred-gw
expect blocked "tokens not in the environment"   sh -c 'env | grep -q fake-token'
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

# Host files still in the old ~/.config/ai-envelope/ only: the start must stop
mv "$HOME/.config/ai-sandbox" "$HOME/.config/ai-sandbox.moved"
mkdir -p "$HOME/.config/ai-envelope"
if "${INIT_ENV[@]}" bash initialize.sh > "$TMP/init-rename.log" 2>&1; then
  fail "initialize.sh started with only ~/.config/ai-envelope/"
elif grep -q 'mv ~/.config/ai-envelope ~/.config/ai-sandbox' "$TMP/init-rename.log"; then
  echo "  ok    old ~/.config/ai-envelope/ stops the start with the mv hint"
else
  cat "$TMP/init-rename.log"; fail "initialize.sh failed, but without the rename hint"
fi
rmdir "$HOME/.config/ai-envelope"
mv "$HOME/.config/ai-sandbox.moved" "$HOME/.config/ai-sandbox"

# cred-gw configured without its token: the start must stop
rm "$HOME/.config/ai-sandbox/cred-gw/gitlab-token"
if "${INIT_ENV[@]}" bash initialize.sh > "$TMP/init3.log" 2>&1; then
  fail "initialize.sh started without the gitlab token"
elif grep -q 'gitlab is configured, but .*gitlab-token is missing' "$TMP/init3.log"; then
  echo "  ok    a missing token stops the start"
else
  cat "$TMP/init3.log"; fail "initialize.sh failed, but not because of the missing token"
fi

echo
if [ "$FAILED" -eq 0 ]; then echo "sandbox-test: all checks passed"; else echo "sandbox-test: FAILED"; exit 1; fi
