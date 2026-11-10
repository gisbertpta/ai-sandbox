# Agent modules

Each folder here is one agent CLI that can be installed in the sandbox. A
project picks its modules in the project layer's `modules` file (one name per
line). Without that file, you get `claude` only.

| Module | Command | Auth |
|---|---|---|
| `claude` | `claude` | Claude login inside the sandbox, state in host `~/.claude-devcontainer` + `~/.claude-devcontainer.json` (not your host `~/.claude`) |
| `copilot` | `copilot` | Fine-grained PAT from a host file (see [below](#copilot-token)) |
| `ntfy` | – | No image changes: allowlists `ntfy.sh` and mounts a random topic for the Claude Stop hook (see [below](#ntfy-notifications)) |
| `cred-gw` | – (`git`, `glab`, `curl`, … against Jira/GitLab) | Jira/GitLab tokens stay in a separate gateway container that enforces the project's rules (see [below](#cred-gw-credential-gateway)) |

## What a module contains

| File | Required | Purpose |
|---|---|---|
| `Dockerfile` | no | Starts `FROM ${BASE_IMAGE}` (the previous image in the chain). Installs the CLI, renames it to `<cli>.real`, and installs a wrapper as `<cli>` that ends with `exec run-guarded <cli>.real "$@"`. Build context: the module folder. Ends with `USER dev`. Without a Dockerfile the module only adds mounts/allowlist. |
| `allowlist.txt` | no | Proxy domains the CLI needs. |
| `mounts` | no | Volumes for the devcontainer service, one compose short-syntax entry per line. `${HOME}` is allowed; relative paths resolve against `.devcontainer/`. |
| `prereqs` | no | Host paths that must exist: `<file\|dir> <path> <hint>` (relative paths: workspace root). `initialize.sh` fails with the hint if one is missing. Every mount source belongs here, otherwise Docker creates a directory in its place. |
| `build-contexts` | no | Named build contexts for the `Dockerfile`: `<name> <host dir>` per line, used as `COPY --from=<name>`. For files outside the module folder (e.g. the sandbox CA certificate). |
| `build-secrets` | no | Secret files for the `Dockerfile`: `<id> <host file>` per line, read with `RUN --mount=type=secret,id=<id>`. The secret exists only during that `RUN` and never ends up in the image (e.g. a token to fetch plugins at build time). `initialize.sh` stops if the file is missing or empty; list it in `prereqs` too, for the hint. Changing a secret doesn't invalidate the build cache: rebuild with `NO_CACHE=1`. |
| `compose.yml` | no | Own services and networks (e.g. the `cred-gw` service), included into the compose stack via `include:`. It can't change the `devcontainer` service; that's what `configure` is for. Relative paths resolve against the module folder. Needs Docker Compose 2.20 or later. |
| `configure` | no | Host-side hook, run after the prereqs are checked (cwd `.devcontainer/`). Reads the project's settings for the module, checks them and prints what the devcontainer needs, one per line: `network <name>`, `extra_host <host> <ip>`, `no_proxy <host>`. May write the values its `compose.yml` interpolates to `$MODULE_ENV`. A non-zero exit stops the start. |

## How they are joined

`initialize.sh` (on the host, before every start):

1. Reads `project/modules`.
2. Checks every module's `prereqs`.
3. Builds the image chain: `ai-sandbox-base` → `ai-sandbox-mod-claude` →
   `ai-sandbox-mod-claude-copilot` → … The tag names the whole chain, so
   projects with different module sets don't overwrite each other.
4. Generates `.devcontainer/.generated/` (gitignored):
   - `compose.yml`: `include:` of the modules' `compose.yml`, and the compose
     override with `BASE_IMAGE` (the last chain image), what the `configure`
     hooks asked for,
     all module mounts, and the read-only protection (`.git/config`,
     `.git/hooks`, `protected-paths`). It is merged between `docker-compose.yml` and `project/compose.yml`.
   - `allowlist-modules.txt`: all module allowlists, read by the proxy.

The project layer's `Dockerfile` starts `FROM` the last chain image, so it
gets every enabled CLI.

To add a module, create a folder with the files above and list it in a
project's `modules` file. Copy an existing module as a starting point.

## Claude settings

Claude's sandbox settings are **managed settings**, so they have the highest
precedence, apply in every directory (also when Claude starts in a nested repo,
where `/workspace/.claude/settings.json` would not be loaded), and the agent
can't change them. Deny rules and hooks also apply in bypass-permissions mode.
They come in two parts:

| Part | File | Lands in the container as |
|---|---|---|
| Generic (this module) | `claude/managed-settings.json`, baked into the image, root-owned | `/etc/claude-code/managed-settings.json` |
| Project (optional) | `claude-settings.json` in the project layer, mounted read-only by `initialize.sh` | `/etc/claude-code/managed-settings.d/50-project.json` |

The generic part only holds what every project wants: the Stop hook for ntfy
and a read block for `.env` files. Everything project-specific (more secret
paths, hooks for the project's own guard scripts, attribution) goes into the
project layer. Claude merges the drop-in over the generic file: single values
are replaced, lists such as `permissions.deny` and `hooks` are combined.

The project file must be strict JSON (no comments). `initialize.sh` checks it
and refuses to start otherwise, because Claude itself would refuse to start.
Example:

```json
{
  "attribution": { "commit": "", "pr": "", "sessionUrl": false },
  "permissions": {
    "deny": ["Read(**/secrets.yml)", "Read(**/build-secret/**)", "Read(~/.jira_env)"]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|MultiEdit",
        "hooks": [{ "type": "command", "command": "bash \"$CLAUDE_PROJECT_DIR\"/scripts/agent/guards/check-command.sh", "timeout": 30 }]
      }
    ]
  }
}
```

`Read(...)` deny rules only stop Claude's Read tool, not `cat` in a Bash call.
They prevent accidents; masking (`excluded-files`) is the actual protection.

Bypass-permissions mode comes from the `claude` wrapper
(`--dangerously-skip-permissions`); auto-update is off (`DISABLE_AUTOUPDATER=1`),
updates come with `NO_CACHE=1 initialize.sh`. After changing either settings
file, rebuild the container.

## cred-gw (credential gateway)

Lets the agent use Jira and GitLab without ever holding a token. The gateway
is a separate container with the tokens; the agent's requests reach it under
the real host names and it applies the project's rules before it adds the
token and forwards them:

- **Jira:** reads only issues with the project's label (plus the metadata of
  the listed projects); writes (edit, comment, transition, create) only when
  enabled.
- **GitLab:** fetch and API reads only for the listed projects; push and merge
  requests only for the write projects, and only to matching branches (default
  `feature/`, `fix/`, `bugfix/`, `chore/`, `docs/`, `refactor/`, `test/`).

How it works: inside the devcontainer, `jira.example.com` resolves to the
gateway (`extra_hosts`) and is in `NO_PROXY`, so `git`, `glab` or `curl` connect
to it directly on the internal `cred-gw` network. The gateway serves a
certificate from the sandbox CA for those names, which the module's image layer
trusts. Only the gateway can reach the real hosts, and only it has the tokens
and the TLS key. Everything else still goes through the proxy.

| File | Purpose |
|---|---|
| `gateway/` | The gateway: `server.py` (HTTPS front, routing by host), `jira_policy.py`, `gitlab_policy.py`, their tests, `Dockerfile` |
| `compose.yml` | The `cred-gw` service and its networks |
| `Dockerfile` + `build-contexts` | Devcontainer layer: trusts the sandbox CA |
| `configure` | Reads the project's `cred-gw.env`, checks tokens and certificate, sets `extra_hosts`/`NO_PROXY` |
| `make-certs.sh` | Creates the sandbox CA and the gateway certificate on the host |
| `cred-gw.env.example` | Template for the project layer's `cred-gw.env` |

### Setup

1. Enable the module in the project layer's `modules` (next to `claude`) and
   copy the policy template into the layer:
   ```bash
   cp .devcontainer/modules/cred-gw/cred-gw.env.example <project layer>/cred-gw.env
   ```
   Fill it in: upstream URLs, Jira label and projects, GitLab read/write
   projects. A service is on when its `*_GW_UPSTREAM` is set.
2. Store the tokens on the host, one file per service you configured. Use
   tokens with the smallest scope that works (Jira: a personal access token of
   a user who can see the project; GitLab: `api` + `read_repository` +
   `write_repository` only if pushing is enabled):
   ```bash
   mkdir -p ~/.config/ai-sandbox/cred-gw && chmod 700 ~/.config/ai-sandbox/cred-gw
   (umask 077; printf 'Jira token: '; read -rs t && echo && printf '%s\n' "$t" > ~/.config/ai-sandbox/cred-gw/jira-token)
   (umask 077; printf 'GitLab token: '; read -rs t && echo && printf '%s\n' "$t" > ~/.config/ai-sandbox/cred-gw/gitlab-token)
   ```
3. Create the sandbox CA and the gateway certificate for the upstream hosts:
   ```bash
   bash .devcontainer/modules/cred-gw/make-certs.sh jira.example.com gitlab.example.com
   ```
   Existing files are kept. To add a host later, delete
   `~/.config/ai-sandbox/sandbox-ca/cred-gw/tls.*` and run it again with all
   hosts (the CA stays, so no image rebuild is needed for the trust).
4. If the upstream uses a company CA, put its bundle into the project layer and
   set `CRED_GW_UPSTREAM_CA=/run/cred-gw/project/<file>` in `cred-gw.env`.
5. Rebuild the container. `initialize.sh` prints `[cred-gw] Gateway 172.30.99.10
   for ...`; `.devcontainer/compose.sh logs -f cred-gw` shows every decision
   (`pass`/`deny`, host, method, path; never bodies or query strings).

Inside the container, point the tools at the real host names as usual, e.g.
`git clone https://gitlab.example.com/group/my-repo.git`. Git asks for no
password: the gateway adds the token. A refused push shows up in git's output
as `! [remote rejected] ... (cred-gw: ...)`.

**Several projects at once:** each project with cred-gw needs its own subnet:
set `CRED_GW_SUBNET` in `cred-gw.env` (default `172.30.99.0/28`).

**Migrating from an older setup** (cred-gw wired in the project layer's
`compose.yml`, tokens in `~/.config/ai-sandbox/{jira,gitlab}-token`): remove
the `cred-gw` service, its networks, `extra_hosts`, `NO_PROXY` and the
`sandbox-ca` build context from the layer, move the `*_GW_*` environment
variables into `cred-gw.env`, and move the tokens:
`mkdir -p ~/.config/ai-sandbox/cred-gw && mv ~/.config/ai-sandbox/{jira,gitlab}-token ~/.config/ai-sandbox/cred-gw/`.
The certificates in `~/.config/ai-sandbox/sandbox-ca/` stay where they are.

## ntfy notifications

Enable the `ntfy` module, then create a random topic on the host and
subscribe to it in the ntfy app:

```bash
mkdir -p ~/.config/ai-sandbox
(umask 077; openssl rand -hex 16 > ~/.config/ai-sandbox/ntfy-topic)
cat ~/.config/ai-sandbox/ntfy-topic
```

The hook sends only a fixed text ("Task finished in <dir>"), no message
content. Without the module the hook is a no-op and `ntfy.sh` is blocked.

## Copilot token

The `copilot` module authenticates with a fine-grained personal access token,
not with `/login`. The token is useless for anything except Copilot requests,
so it does little harm if the agent reads it (which it can, like any
credential the CLI uses).

1. On GitHub: **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
   - Resource owner: **your personal account** (organization-owned tokens are
     not supported by the CLI).
   - Repository access: **Public repositories (read-only)**, i.e. no access to
     private repos.
   - Account permissions: **Copilot Requests** → Read-only. Nothing else.
   - Expiration: as long as your org's policy allows.
2. Store it on the host:
   ```bash
   mkdir -p ~/.config/ai-sandbox
   (umask 077; printf 'Token: '; read -rs t && echo && printf '%s\n' "$t" > ~/.config/ai-sandbox/copilot-token)
   mkdir -p ~/.copilot-devcontainer
   ```
3. Rebuild the container, then run `copilot`. It must start without asking
   you to log in.

How it gets in: the file is mounted read-only at `/run/secrets/copilot-token`.
The `copilot` wrapper exports it as `COPILOT_GITHUB_TOKEN` for the Copilot
process only, so it is not in the environment of your shell or of other tools.

When the token expires, replace the file. No rebuild is needed; the next
`copilot` start reads the new token.

`~/.copilot-devcontainer` holds Copilot's state (trusted folders, sessions)
across rebuilds. It is a separate directory on purpose, so the agent can't
touch a host `~/.copilot`.

If your Copilot seat comes from an organization that blocks fine-grained PATs,
`copilot` still asks for a login. Then the only way is `/login` inside the
container: temporarily add `github.com` to `allowlist.txt`, run `/login` once
(the token lands in `~/.copilot-devcontainer`), and remove `github.com` again.
That OAuth token has broader access than the PAT.
