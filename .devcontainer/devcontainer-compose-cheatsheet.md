# Devcontainer compose cheat sheet (ai-sandbox)

All commands run **on the host**, from the repo root (the folder containing `.devcontainer/`).
Not from inside the devcontainer: it has no Docker access and mounts `.devcontainer/` read-only.

## Ground rules

- Use `.devcontainer/compose.sh <cmd>` instead of bare `docker compose`. It merges all three files:
  `docker-compose.yml` + `.generated/compose.yml` + `project/compose.yml`.
  A bare `docker compose` only sees part of the config and gets the project name wrong.
- **Start** via VS Code or `devcontainer up`, not `compose.sh up`. Only those run `initialize.sh`
  (base/module images, `.generated/`) and `check-exclusions`.
- Compose project name: whatever `name:` in `project/compose.yml` says, e.g. `ai-sandbox-myproject`.
  Below, `<name>` stands for it; containers are called `<name>-devcontainer-1`, `<name>-proxy-1`, ….
- Services: `devcontainer`, `proxy` (squid egress allowlist), and `cred-gw` (Jira/GitLab token gateway)
  when the project enables the `cred-gw` module.
- `.generated/compose.yml` missing → `bash .devcontainer/initialize.sh` once.

## Start, stop, rebuild

| Task | Command |
| --- | --- |
| Start (build if needed) | `devcontainer up --workspace-folder .` |
| Shell as `dev` | `devcontainer exec --workspace-folder . zsh` |
| Stop + remove all containers | `.devcontainer/compose.sh down` |
| Stop, keep containers | `.devcontainer/compose.sh stop` |
| Rebuild after editing `.devcontainer/` or the project layer | `devcontainer up --workspace-folder . --remove-existing-container` |
| VS Code equivalent | *Dev Containers: Rebuild Container* |
| Update agent CLIs / base image | `NO_CACHE=1 bash .devcontainer/initialize.sh`, then rebuild |
| Full clean rebuild | `NO_CACHE=1 bash .devcontainer/initialize.sh` then `.devcontainer/compose.sh build --no-cache`, then start |
| Rebuild one service image | `.devcontainer/compose.sh build cred-gw` (or `proxy`, `devcontainer`) |
| Recreate one service with new image/config | `.devcontainer/compose.sh up -d --no-deps --build cred-gw` |

`down` keeps the `shell-history` volume. `down -v` deletes it (zsh history lost).

## Status and logs

| Task | Command |
| --- | --- |
| Running containers | `.devcontainer/compose.sh ps` |
| Including stopped ones | `.devcontainer/compose.sh ps -a` |
| Follow proxy log (blocked domains) | `.devcontainer/compose.sh logs -f proxy` |
| Follow cred-gw log | `.devcontainer/compose.sh logs -f cred-gw` |
| Last 100 lines, all services | `.devcontainer/compose.sh logs --tail 100` |
| Resource usage | `docker stats` (filter by `<name>-`) |
| Images built for this project | `.devcontainer/compose.sh images` |

Proxy log hint: `TCP_DENIED/403 CONNECT some.domain:443` → domain is not on an allowlist.

## Inspect the effective config

| Task | Command |
| --- | --- |
| Fully merged config (variables resolved) | `.devcontainer/compose.sh config` |
| Only validate, no output | `.devcontainer/compose.sh config -q` |
| List services | `.devcontainer/compose.sh config --services` |
| List volumes | `.devcontainer/compose.sh config --volumes` |
| Networks of the devcontainer | `docker inspect <name>-devcontainer-1 --format '{{json .NetworkSettings.Networks}}'` |

`config` prints resolved paths and environment; it shows no secret contents, but don't paste
it anywhere public anyway.

## Proxy / allowlist maintenance

| Task | Command |
| --- | --- |
| Apply edited `proxy/allowlist.txt` or `project/allowlist.txt` | `.devcontainer/compose.sh restart proxy` |
| Apply edited module allowlists | `bash .devcontainer/initialize.sh`, then `.devcontainer/compose.sh restart proxy` |
| Check squid config / allowlist syntax | `.devcontainer/compose.sh run --rm proxy squid -k parse -f /etc/squid/squid.conf` |

Allowlists are mounted, not baked in: restart is enough. `squid.conf` or `proxy/Dockerfile`
changes need `build proxy` + `up -d --no-deps proxy`.

## cred-gw maintenance

| Task | Command |
| --- | --- |
| After editing `modules/cred-gw/gateway/*.py` or the policy in `project/cred-gw.env` | `.devcontainer/compose.sh up -d --no-deps --build cred-gw` |
| After changing an upstream host in `project/cred-gw.env` | rebuild the devcontainer (host names go into its `extra_hosts`/`NO_PROXY`) |
| After rotating `~/.config/ai-sandbox/cred-gw/{jira,gitlab}-token` | `.devcontainer/compose.sh up -d --no-deps --force-recreate cred-gw` |
| Run the policy tests (host, stdlib only; tests are not in the image) | `cd .devcontainer/modules/cred-gw/gateway && python3 -m unittest -v` |

Secrets are read when the container is created, so a plain `restart` may not pick up a new
token file; `--force-recreate` does.

## Exec and one-off commands

| Task | Command |
| --- | --- |
| Shell in devcontainer as `dev` (via compose) | `.devcontainer/compose.sh exec -u dev devcontainer zsh` |
| Shell in proxy | `.devcontainer/compose.sh exec proxy sh` |
| One-off command, container removed after | `.devcontainer/compose.sh run --rm <service> <cmd>` |
| Copy a file out of the devcontainer | `docker cp <name>-devcontainer-1:/path ./local` |

Without `-u dev`, `exec` runs as root. Prefer `devcontainer exec`; it also applies `remoteEnv`.

## Switch project layer

```bash
.devcontainer/compose.sh down                      # BEFORE switching: compose.sh finds containers by name:
ln -sfn ../other-repo/ai-sandbox .devcontainer/project
devcontainer up --workspace-folder .
```

## Cleanup

| Task | Command |
| --- | --- |
| Remove containers + project network | `.devcontainer/compose.sh down` |
| Also remove orphans (services removed from compose) | `.devcontainer/compose.sh down --remove-orphans` |
| Also remove volumes (shell history) | `.devcontainer/compose.sh down -v` |
| Also remove images built by compose | `.devcontainer/compose.sh down --rmi local` |
| Remove dangling build cache | `docker builder prune` |
| List base/module images | `docker images 'ai-sandbox-*'` |

Avoid `docker system prune -a` and host-wide `docker rm $(docker ps -aq)`: they hit every
container and image on the host, not just this project.

## Troubleshooting quick map

| Symptom | First command |
| --- | --- |
| `pull access denied for ai-sandbox-base` / `ai-sandbox-mod-…` | `bash .devcontainer/initialize.sh` |
| `.generated/compose.yml missing` | `bash .devcontainer/initialize.sh` |
| `secret …: file not found` | check `~/.build-secrets/*` exist on host |
| Mounted path is a directory instead of a file | create the host file (e.g. `echo '{}' > ~/.claude-devcontainer.json`), then rebuild |
| Network blocked inside container | `.devcontainer/compose.sh logs -f proxy` |
| Proxy won't start | `.devcontainer/compose.sh logs proxy`, then `squid -k parse` (see above) |
| Jira/GitLab calls fail | `.devcontainer/compose.sh logs -f cred-gw` |
| `Pool overlaps with other one` (cred-gw network) | set another `CRED_GW_SUBNET` in `project/cred-gw.env` (e.g. `172.30.98.0/28`); `down`, start again |
| Masked-file check shows WARN | compare volumes in `project/compose.yml` with `project/excluded-files`, rebuild |
