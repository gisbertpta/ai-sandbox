# Agent modules

Each folder here is one agent CLI that can be installed in the sandbox. A
project picks its modules in the project layer's `modules` file (one name per
line). Without that file, you get `claude` only.

| Module | Command | Auth |
|---|---|---|
| `claude` | `claude` | Claude login inside the sandbox, state in host `~/.claude-devcontainer` + `~/.claude-devcontainer.json` (not your host `~/.claude`) |
| `copilot` | `copilot` | Fine-grained PAT from a host file (see [below](#copilot-token)) |
| `ntfy` | – | No image changes: allowlists `ntfy.sh` and mounts a random topic for the Claude Stop hook (see [below](#ntfy-notifications)) |

## What a module contains

| File | Required | Purpose |
|---|---|---|
| `Dockerfile` | no | Starts `FROM ${BASE_IMAGE}` (the previous image in the chain). Installs the CLI, renames it to `<cli>.real`, and installs a wrapper as `<cli>` that ends with `exec run-guarded <cli>.real "$@"`. Build context: the module folder. Ends with `USER dev`. Without a Dockerfile the module only adds mounts/allowlist. |
| `allowlist.txt` | no | Proxy domains the CLI needs. |
| `mounts` | no | Volumes for the devcontainer service, one compose short-syntax entry per line. `${HOME}` is allowed; relative paths resolve against `.devcontainer/`. |
| `prereqs` | no | Host paths that must exist: `<file\|dir> <path> <hint>` (relative paths: workspace root). `initialize.sh` fails with the hint if one is missing. Every mount source belongs here, otherwise Docker creates a directory in its place. |

## How they are joined

`initialize.sh` (on the host, before every start):

1. Reads `project/modules`.
2. Checks every module's `prereqs`.
3. Builds the image chain: `ai-envelope-base` → `ai-envelope-mod-claude` →
   `ai-envelope-mod-claude-copilot` → … The tag names the whole chain, so
   projects with different module sets don't overwrite each other.
4. Generates `.devcontainer/.generated/` (gitignored):
   - `compose.yml`: compose override with `BASE_IMAGE` (the last chain image),
     all module mounts, and the read-only git config of the working repos. It is
     merged between `docker-compose.yml` and `project/compose.yml`.
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

## ntfy notifications

Enable the `ntfy` module, then create a random topic on the host and
subscribe to it in the ntfy app:

```bash
mkdir -p ~/.config/ai-envelope
(umask 077; openssl rand -hex 16 > ~/.config/ai-envelope/ntfy-topic)
cat ~/.config/ai-envelope/ntfy-topic
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
   mkdir -p ~/.config/ai-envelope
   (umask 077; read -rs -p 'Token: ' t && printf '%s\n' "$t" > ~/.config/ai-envelope/copilot-token)
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
