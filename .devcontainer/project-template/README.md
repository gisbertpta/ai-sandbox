# Project layer template

Copy this folder to start a new project layer. Every file except `modules`
and `prereqs` is required (`initialize.sh` checks for them), but each one may
contain nothing but comments. Copied unchanged, the template gives you the
plain generic sandbox with Claude Code: no extra tools, no masks, no extra
domains.

| File | Purpose |
|---|---|
| `compose.yml` | Merged on top of `.devcontainer/docker-compose.yml`: compose `name`, masking mounts, build secrets, env vars. |
| `Dockerfile` | `FROM` the generic base image; adds the project toolchain and dependencies. |
| `Dockerfile.dockerignore` | Allowlist of workspace files the `Dockerfile` may `COPY`. The build context is the whole workspace. |
| `excluded-files` | Paths that must be masked. `check-exclusions` checks them. |
| `allowlist.txt` | Extra proxy domains. |
| `prereqs` | Optional. Repos and host files that must exist (`initialize.sh` checks them). List every repo whose secrets are masked. |
| `modules` | Optional. Agent modules to install (`claude`, `copilot`, see `.devcontainer/modules/`). Missing → `claude` only. |
| `claude-settings.json` | Optional. Project-specific Claude managed settings (deny rules, hooks, attribution), merged over the module's generic ones. Strict JSON. See [Claude settings](../modules/README.md#claude-settings). |
| `dummies/` | Placeholder copies of masked paths, mirroring the path: `dummies/<repo>/<path>`. |

See "Starting a new project" in the envelope's [README](../../README.md).
