# AI Sandbox (devcontainer)

A devcontainer for running agent CLIs (Claude Code, GitHub Copilot CLI) with
permission prompts skipped, in a
way that limits what the agent can **see** (secrets are masked) and where it can
**send** things (egress is allowlisted).

This folder holds the **generic** part and the **agent modules**
([`modules/`](modules/README.md)). Everything project-specific comes from the
project layer in the slot `.devcontainer/project` (see the
[top-level README](../README.md) for the layers and how to start a new project).

> Edit everything in this folder **from the host**. Inside the container,
> `.devcontainer/` is mounted read-only on purpose.

---

## How it works

```
 host (WSL)                                     docker
 ─────────────                                  ──────────────────────────────────────────────
 /workspace  ─────── bind mount ──────────►   ┌─ devcontainer ───────────────┐
 .devcontainer/ ──── bind mount (ro) ─────►   │ user: dev (uid 1000)         │
 project dummies ─── bind mount (ro) ─────►   │ cap_drop ALL                 │
                                              │ no-new-privileges            │
                                              │ HTTPS_PROXY=http://proxy:3128│
                                              └──────────────┬───────────────┘
                                                 network "sandbox" (internal, no gateway)
                                              ┌──────────────┴───────────────┐
                                              │ proxy (squid)                │
                                              │ allows: proxy/allowlist.txt  │
                                              │ + modules' allowlist.txt     │
                                              │ + project/allowlist.txt      │
                                              └──────────────┬───────────────┘
                                                 network "egress"
                                                          ▼
                                                      internet
```

There are four layers of protection:

1. **Secret masking.** Real secrets in the workspace are covered by read-only bind
   mounts: either placeholder "dummy" files or `/dev/null`. The agent sees the dummy,
   not the real file. The paths come from the project layer.
2. **Exclusion check.** `check-exclusions` checks that every path in
   `project/excluded-files` is really masked. It runs when the container starts,
   and every agent command (`claude`, `copilot`) runs it too: the agent refuses to
   start if anything is unmasked, or if there is no project layer at all.
3. **Egress allowlist.** The devcontainer has no direct route to the internet.
   Its only way out is the `proxy` container, which allows HTTPS to the domains in
   `proxy/allowlist.txt`, the enabled modules' `allowlist.txt` and
   `project/allowlist.txt`, and blocks everything else.

4. **No write path to the host.** Everything that the host later executes or
   trusts is read-only in the container: the sandbox config, the project layer,
   the git config and hooks of all repos, the agent instructions. Agent homes
   are sandbox-only directories. See [Security notes](#security-notes--known-gaps).

The image **build** is not affected by the proxy. It runs on the normal Docker
network, so e.g. `uv sync` against a private index works during the build.

### Start sequence

1. **Host:** `initialize.sh` (`initializeCommand`) checks that the slot
   `project/` holds a complete layer, reads the enabled modules from
   `project/modules` (default: `claude`) and checks the host prerequisites
   (generic + per module). Then it builds `base/` as `ai-envelope-base:latest`,
   stacks one image per module on top (e.g. `ai-envelope-mod-claude-copilot:latest`)
   and generates `.generated/compose.yml` (module mounts, read-only protection of
   `.git/config`/`hooks` and `protected-paths` in every working repo, see
   [Security notes](#security-notes--known-gaps)) + `.generated/allowlist-modules.txt`.
   It warns about protected paths and working repos that are new since the last
   start (`.generated/protection-state`).
2. **Host:** compose merges `docker-compose.yml` + `.generated/compose.yml` +
   `project/compose.yml` and builds `project/Dockerfile` (`FROM` the last module
   image). The build context is the workspace root, filtered by
   `project/Dockerfile.dockerignore`.
3. **Container:** `check-exclusions` (`postStartCommand`).

---

## Files

| File | Purpose |
|---|---|
| `devcontainer.json` | Entry point for VS Code. Runs `initialize.sh`, loads the compose files, sets the user and extensions. |
| `docker-compose.yml` | **The generic config:** build, mounts, capabilities, networks, proxy env. |
| `initialize.sh` | Runs on the host before start: checks the slot and prerequisites, builds base + module images, generates `.generated/`. |
| `tests/sandbox-test.sh` | End-to-end test of masks and read-only protection (see [Testing](#testing)). |
| `protected-paths` | Paths in every working repo (and the envelope root) that host tools run by themselves; mounted read-only. |
| `compose.sh` | `docker compose` with all compose files. Use it instead of bare `docker compose`. |
| `base/Dockerfile` | Generic image: Node, zsh, vim, git, rg, fd, jq, fzf, shellcheck, python3, uv, `dev` user. No agent CLI. |
| `base/check-exclusions.sh` | The check. Installed in the image as `/usr/local/bin/check-exclusions`. |
| `base/run-guarded.sh` | Installed as `run-guarded`. Runs the check, then the given command. Every agent wrapper calls it. |
| `base/.zshrc` | Shell config for the `dev` user (vi mode, fzf, autosuggestions, syntax highlighting). |
| `base/.vimrc` | Minimal vim config for the `dev` user. |
| `modules/claude/` | Claude Code: install + wrapper (bypass mode), generic managed settings (Stop hook, `.env` read block), allowlist, sandbox home mounts. |
| `modules/copilot/` | GitHub Copilot CLI: install + wrapper, allowlist, token + state mounts. |
| `modules/cred-gw/` | Opt-in: credential gateway for Jira/GitLab (own service), CA trust, `make-certs.sh`. |
| `modules/ntfy/` | Opt-in: allowlists `ntfy.sh` and mounts the topic for the Claude Stop hook. |
| `.generated/` | Not tracked: written by `initialize.sh` from the enabled modules. |
| `proxy/` | Squid egress proxy: `Dockerfile`, `squid.conf`, generic `allowlist.txt`. |
| `project-template/` | Template for new project layers. |
| `project` | **Slot**, not tracked: symlink to (or copy of) the active project layer. |

A project layer contains `compose.yml`, `Dockerfile`, `Dockerfile.dockerignore`,
`excluded-files`, `allowlist.txt`, `dummies/` and optionally `modules`. See
[`project-template/README.md`](project-template/README.md). How modules are
built and what they contain: [`modules/README.md`](modules/README.md).

---

## Prerequisites (host)

These must exist on the host before the first build. `initialize.sh` checks them
(each module lists its own in `modules/<name>/prereqs`):

| Path | Needed by | Used for |
|---|---|---|
| `.devcontainer/project` | always | The project layer (symlink or copy). |
| `~/.gitconfig` | always | Git identity (read-only). **Must exist as a file.** If a mounted file is missing, Docker creates a *directory* with that name instead. |
| `~/.claude-devcontainer/` | `claude` | Sandbox-only Claude home (login, sessions). **Not** your host `~/.claude`. |
| `~/.claude-devcontainer.json` | `claude` | Sandbox-only Claude state file. Must exist as a file: `echo '{}' > ~/.claude-devcontainer.json`. |
| `~/.config/ai-envelope/copilot-token` | `copilot` | Fine-grained PAT. See [Copilot token](modules/README.md#copilot-token). |
| `~/.copilot-devcontainer/` | `copilot` | Copilot config/state, separate from a host `~/.copilot`. |
| `~/.config/ai-envelope/ntfy-topic` | `ntfy` | Random topic name: `(umask 077; openssl rand -hex 16 > ~/.config/ai-envelope/ntfy-topic)`. Subscribe to it in the ntfy app. |

The project layer may need more (e.g. `~/.build-secrets/` for build
credentials). Its README lists them.

You also need Docker with Compose v2 (`docker compose version`) and BuildKit,
which is on by default.

---

## Usage

### Start / rebuild

- VS Code: **Dev Containers: Reopen in Container**. After changing anything in
  `.devcontainer/` or the project layer: **Dev Containers: Rebuild Container**.
- CLI (devcontainer CLI, `npm install -g @devcontainers/cli`). Run it on the host
  from the repo root, i.e. the folder that contains `.devcontainer/`:
  ```bash
  devcontainer up --workspace-folder .                              # build if needed + start devcontainer AND proxy
  devcontainer exec --workspace-folder . zsh                        # shell as user 'dev'
  devcontainer up --workspace-folder . --remove-existing-container  # after changing .devcontainer/ or layer files
  .devcontainer/compose.sh down                                     # stop both containers
  ```
  The proxy is part of the compose project and starts automatically. You never
  start it separately.
- Avoid plain `docker compose up` and `.devcontainer/compose.sh up`. They start the
  containers but skip `devcontainer.json`: no `initialize.sh` (the base image
  may be stale or missing), no `check-exclusions` at start, and `exec` puts you
  in as root unless you pass `-u dev`.
- Update the agent CLIs / the base image: `NO_CACHE=1 bash .devcontainer/initialize.sh`,
  then rebuild the container.
- After starting, the terminal should show one `OK` line per masked path, e.g.:
  ```
  [exclusions] Checking masked files...
    OK (dummy)   my-repo/config
    OK (empty)   my-repo/.env
  [exclusions] All checks passed.
  ```

### Switch to another project layer

```bash
.devcontainer/compose.sh down
ln -sfn ../other-repo/ai-envelope .devcontainer/project
devcontainer up --workspace-folder .
```

Run `down` **before** switching: `compose.sh` uses the layer's compose `name:` to
find the containers.

### Run an agent

```bash
claude            # runs the exclusion check first, then Claude Code (bypass-permissions mode)
copilot           # same for GitHub Copilot CLI (if the module is enabled)
```

`claude.real` / `copilot.real` skip the check. Only use them for debugging,
never for real work.

### Enable / disable an agent

Edit `modules` in the project layer (one module name per line, see
[`modules/README.md`](modules/README.md)), create the module's host
prerequisites, then rebuild the container.

### Mask an additional secret file

In the project layer:

1. Add the path (relative to `/workspace`) to `excluded-files`.
2. Add a read-only volume in `compose.yml` → `services.devcontainer.volumes`:
   ```yaml
   - /dev/null:/workspace/my-repo/path/to/secret.env:ro                                   # blank it
   - ./project/dummies/my-repo/path/secret.yml:/workspace/my-repo/path/secret.yml:ro       # or a dummy
   ```
   For a dummy, create it under `dummies/` at the **same path** as the masked one,
   with **placeholder values only**.
3. Rebuild. `check-exclusions` must show `OK` for the new path.

Prefer masking **whole directories** over single files. With single files, new or
nested files in that directory aren't covered.

### Allow an additional domain

1. Generic (needed by every project): add it to `proxy/allowlist.txt`.
   Needed by an agent CLI: `modules/<name>/allowlist.txt` (then run
   `bash .devcontainer/initialize.sh` to regenerate the merged list).
   Project-specific: add it to the layer's `allowlist.txt`.
   A leading dot includes subdomains: `.example.com`.
2. Restart the proxy, from the host, in the repo root (the lists are mounted,
   no rebuild needed):
   ```bash
   .devcontainer/compose.sh restart proxy
   ```

Keep the lists short. Every domain on them is a place the agent can send data to.

---

## Debugging

### Which container is which?

```bash
.devcontainer/compose.sh ps
#   <name>-devcontainer-1
#   <name>-proxy-1          (<name> = 'name:' in project/compose.yml)
```

### Start fails before anything is built

`initialize.sh` prints `[init] ERROR: ...`:
- `no project layer` / `broken symlink` → link a layer into the slot (see the
  [top-level README](../README.md#starting-a-new-project)). Symlink targets are
  relative to `.devcontainer/`.
- `project layer is missing '…'` → every layer needs all files of
  `project-template/`, even if they only hold comments.
- `… must exist as a file` / `as a directory` → see [Prerequisites](#prerequisites-host).
  The hint in parentheses names the module that needs it.
- `unknown module '…'` → a name in `project/modules` has no folder in `modules/`.
- `compose.sh: … .generated/compose.yml missing` → run `bash .devcontainer/initialize.sh` once.
- `… must exist … (project layer: …)` → a repo from the layer's `prereqs` is not
  cloned (or under another name), or a build secret is missing.

### "Something can't reach the network"

1. Watch the proxy log while you retry:
   ```bash
   .devcontainer/compose.sh logs -f proxy
   ```
   `TCP_DENIED/403 CONNECT some.domain:443` → the domain isn't on the allowlist.
   Add it if it's legitimate (see above).
2. From inside the container:
   ```bash
   env | grep -i proxy                              # proxy vars set?
   curl -sI https://api.anthropic.com | head -1     # expect an HTTP status → allowed
   curl -sI https://example.com                     # expect 403 → blocked by squid
   curl -sI --noproxy '*' https://example.com       # expect failure → no direct route (good)
   ```
   If the last command *succeeds*, the network isolation isn't working. Check that
   the devcontainer is only on the `sandbox` network:
   `docker inspect <name>-devcontainer-1 --format '{{json .NetworkSettings.Networks}}'`
3. A tool ignores the proxy: most tools (curl, git, uv/pip, Claude Code) read
   `HTTPS_PROXY`. Tools that don't will simply fail, which is the intended result.
4. Plain `http://` URLs are blocked entirely. Only HTTPS (port 443) is allowed.

### Proxy container keeps restarting / exits

```bash
.devcontainer/compose.sh logs proxy
.devcontainer/compose.sh run --rm proxy squid -k parse -f /etc/squid/squid.conf
```
`squid -k parse` shows config errors. A typo in one of the allowlists (e.g. a
stray character) is the usual cause. If `project/allowlist.txt` is missing,
Docker creates a directory in its place and squid fails. Recreate it as a file.

### VS Code can't install its server or extensions

VS Code downloads its server and extensions **inside** the container, through the
proxy. If this fails, look for `TCP_DENIED` in the proxy log and add the domain
it's trying to reach to `proxy/allowlist.txt`.

### `check-exclusions` shows WARN / the agent won't start

```
WARN   my-repo/config  <-- not masked!
```
- The mount for that path is missing or wrong in `project/compose.yml`. Compare
  the volume's target with the path in `project/excluded-files`.
- Check what's actually mounted, from inside the container:
  ```bash
  grep /workspace /proc/self/mountinfo | awk '{print $5, $6}'
  ```
- The dummy must be at `project/dummies/<same path>`. For directories, the check
  compares the *entire* directory against the dummy (`diff -rq`).
- `/usr/local/bin/check-exclusions` is baked into the base image. After editing
  `base/check-exclusions.sh` you must **rebuild**. To try an edit without rebuilding:
  `bash /workspace/.devcontainer/base/check-exclusions.sh`.

### Build fails

- `pull access denied for ai-envelope-base` / `ai-envelope-mod-…` → the base or
  module images weren't built. Start via VS Code / `devcontainer up` (runs
  `initialize.sh`), or run `bash .devcontainer/initialize.sh` once.
- `COPY … not found` → the file isn't allowlisted in
  `project/Dockerfile.dockerignore`. (The root `/.dockerignore` excludes everything
  on purpose. It only applies if the layer's file is missing.)
- `secret …: file not found` → a build secret declared in `project/compose.yml`
  is missing on the host.
- Start over completely:
  ```bash
  NO_CACHE=1 bash .devcontainer/initialize.sh   # base + module images
  .devcontainer/compose.sh build --no-cache
  ```

### Claude Code login / state problems

- The sandbox has its own Claude home, so you log in once inside the container
  (not shared with Claude on the host). The first start also asks you to accept
  bypass-permissions mode once.
- `~/.claude-devcontainer.json` became a directory on the host → delete it,
  `echo '{}' > ~/.claude-devcontainer.json`, rebuild.
- Login fails → check the proxy log. Login and token refresh need `.anthropic.com`
  and `.claude.ai`.
- Settings are managed settings: the generic part in the image
  (`modules/claude/managed-settings.json` → `/etc/claude-code/managed-settings.json`),
  the project part from the layer's `claude-settings.json`
  (→ `/etc/claude-code/managed-settings.d/50-project.json`). They apply in every
  directory, also in nested repos. Change them there and rebuild; `/status` in
  Claude shows `(file + drop-ins)` when both are loaded.

### Copilot asks for a login

- The token file is missing or empty: the wrapper prints a warning. See
  [Copilot token](modules/README.md#copilot-token).
- The token is rejected (expired, wrong permission, organization blocks
  fine-grained PATs): check `copilot` output and the token settings on GitHub.
- Requests are blocked: look for `TCP_DENIED` in the proxy log and add the
  domain to `modules/copilot/allowlist.txt`.

### ntfy notification doesn't arrive

- The `ntfy` module must be enabled in `project/modules` (it allowlists `ntfy.sh`
  and mounts the topic file).
- Test inside the container: `echo '{}' | /usr/local/libexec/ai-envelope/notify-ntfy.sh`.

### git can't write `.git/config` / hooks inside the container

On purpose: `.git/config` and `.git/hooks` of every working repo are read-only
in the container, the envelope's whole `.git` too. Commits work; `git config`,
`git remote add`, installing hooks etc. must be done on the host. A repo cloned
while the container runs is protected only after the next rebuild/restart
(`initialize.sh` finds it). Repos with a `.git` *file* (worktrees, submodules)
are not protected; `initialize.sh` warns about them.

Single files mounted read-only (`AGENTS.md`, `CLAUDE.md`, `.gitignore`,
`.dockerignore`, `.git/config`): if your host editor replaces the file instead
of writing it in place, the container keeps seeing the old version until it is
restarted.

---

## Testing

CI (`.github/workflows/ci.yml`) runs on every push to `main` and every pull
request:

| Job | What |
|---|---|
| `lint` | `shellcheck -S warning` on all `*.sh`, strict JSON check (managed settings) |
| `cred-gw` | The policy unit tests (`modules/cred-gw/gateway/`), in `python:3.11-slim` like the gateway image |
| `sandbox` | `tests/sandbox-test.sh`: builds the images, starts the devcontainer in a throwaway workspace with fake repos and a project layer, and checks that masks hold, protected paths can't be changed or renamed away, normal git work still works, the next start warns about new paths, and with the `cred-gw` module: host names reach the gateway, TLS verifies, the policy applies, the tokens stay out of the devcontainer |

Run the end-to-end test on the host after changing `initialize.sh`, the compose
files or the protection:

```bash
bash .devcontainer/tests/sandbox-test.sh             # rebuilds (and retags) the ai-envelope-* images
NO_BUILD=1 bash .devcontainer/tests/sandbox-test.sh  # uses the images you have
```

It uses its own compose project (`sandbox-test`) and a temp directory, and
removes both afterwards; your containers and working repos are not touched.
With `NO_BUILD=1`, it tests the current scripts against your existing images, so
image changes (base, modules) are only covered by a full run.

## Security notes & known gaps

### What is protected

- **What host tools run by themselves stays read-only.** For the agent:
  `.devcontainer/`, the project layer (also when it lives in a working repo),
  the envelope's `.git`, `AGENTS.md`/`CLAUDE.md`, `.gitignore`, `.dockerignore`,
  and in every working repo `.git/config` + `.git/hooks` (no planted hooks,
  `core.fsmonitor`, aliases or filters for your next host `git`), a
  `core.hooksPath` inside the repo (e.g. `.husky/`), and every path in
  [`protected-paths`](protected-paths): `.claude/`, `.mcp.json`, `.vscode/`,
  `.devcontainer/`, `.envrc`, `.pre-commit-config.yaml`. The same list applies
  to the envelope root. A project layer adjusts it in its own `protected-paths`.
- **Read-only can't be undone by renaming.** A mount point can't be renamed, but
  its parent directory can (`mv repo/.git repo/.git-x`, then a fresh `.git` with
  a planted hook). So `initialize.sh` also mounts every directory above a
  protected path onto itself (read-write), e.g. `repo/` and `repo/.git/`. Normal
  work is unaffected; only renaming or moving those directories themselves fails.
- **Symlinks are never mounted.** A protected path that is (or lies under) a
  symlink is reported and left unprotected, so a planted `.vscode -> ~/.ssh`
  can't get a host directory mounted into the sandbox at the next start.
- **Agent homes are sandbox-only** (`~/.claude-devcontainer`,
  `~/.copilot-devcontainer`), never your host `~/.claude` / `~/.copilot`.
  Claude's settings are managed settings (root-owned in the image, the
  project part mounted read-only).
- **Host git credentials stay out:** `GIT_CONFIG_NOSYSTEM=1` makes git ignore
  `/etc/gitconfig`, where VS Code installs its credential-forwarding helper, and
  `remoteEnv` clears `GIT_ASKPASS`, `SSH_AUTH_SOCK`, `BROWSER` and VS Code's IPC
  variables.
- **No direct network, no DNS:** external names don't resolve on the internal
  network, so DNS can't be used as a side channel either.
- **Resources:** `mem_limit: 8g`, `pids_limit: 4096`.

### What is not protected

- **VS Code attached = extra host channels.** VS Code may re-inject `BROWSER`
  and its IPC variables into its own terminals, and its sockets in `/tmp` stay
  usable (e.g. opening a URL in your host browser, which bypasses the proxy).
  For unattended agent runs, use a shell from `devcontainer exec` instead of a
  VS Code terminal. Also set in your **host** VS Code user settings:
  `"dev.containers.copyGitConfig": false`,
  `"dev.containers.gitCredentialHelperConfigLocation": "none"`.
- **Allowlisted domains are exfiltration channels.** `.anthropic.com`,
  `.githubcopilot.com` and `api.github.com` accept uploads to an attacker's
  account if the agent uses the attacker's key; `ntfy.sh` (module `ntfy`)
  accepts anything to any topic. The proxy stops accidents, not a determined,
  prompt-injected agent. Keep the lists short.
- **Tokens in the sandbox are readable by the agent:** the sandbox Claude login
  (`~/.claude-devcontainer/.credentials.json`) and the Copilot PAT. Both are
  sandbox-only and can be revoked separately; the PAT can only make Copilot
  requests. Jira and GitLab tokens never enter the sandbox when you use the
  [`cred-gw` module](modules/README.md#cred-gw-credential-gateway); the agent can
  still do everything the gateway's rules allow.
- **Protected paths that don't exist yet** can't be mounted (Docker would create
  them on the host, owned by root), so the agent can create them, e.g. a new
  `.mcp.json` or `.claude/settings.json` in a repo that has no `.claude/`.
  `initialize.sh` warns at the next start (`... is new since the last start`) and
  protects them from then on. The same goes for a new working repo the agent
  created or cloned: check its `.git/config` and `.git/hooks` before you run git
  there.
- **The rest of the workspace is writable.** The agent can change project code,
  including things you later run on the host (tests, `Makefile`, `package.json`
  scripts, scripts that a protected hook calls; add those to the layer's
  `protected-paths`). And `CLAUDE.md`/`AGENTS.md` of the working repos stay
  writable, so the agent can put instructions there for your next host session.
  Review `git diff` before you run, commit or push anything on the host.
- `check-exclusions` and the agent wrappers protect against **accidents**
  (a forgotten mount), not against a deliberately misbehaving agent (it could run
  `claude.real`). The actual protection is the mounts and the network.
