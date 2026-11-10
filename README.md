# AI dev envelope

A reusable workspace that wraps one or more **working repos** in a sandboxed
devcontainer for running agent CLIs (Claude Code, GitHub Copilot CLI) with
permission prompts skipped.

This repo holds **no project code**. It only holds the parts around the code:
the generic container definition, the agent modules, the egress proxy, secret
masking, and workspace-wide AI instructions. Everything that belongs to a
specific project (toolchain, dependencies, masked secrets, extra domains) lives
in a separate **project layer** that is plugged into a fixed slot.

```
/workspace                        ← this repo (the envelope)
├── .devcontainer/
│   ├── devcontainer.json         entry point: base + modules + project compose files
│   ├── docker-compose.yml        generic: sandbox, proxy, networks, generic mounts
│   ├── initialize.sh             host-side: checks slot + prerequisites, builds images
│   ├── compose.sh                'docker compose' with all compose files
│   ├── protected-paths           paths host tools run by themselves → read-only
│   ├── base/                     generic image: zsh, vim, git, rg, fd, jq, fzf, uv (no agent)
│   ├── modules/                  agent CLIs, each with Dockerfile, allowlist, mounts
│   │   ├── claude/               Claude Code (+ managed settings, Stop hook)
│   │   ├── copilot/              GitHub Copilot CLI
│   │   ├── cred-gw/              opt-in: Jira/GitLab credential gateway (own service)
│   │   └── ntfy/                 opt-in: Stop-hook notification via ntfy.sh
│   ├── proxy/                    squid egress proxy + generic allowlist
│   ├── tests/                    end-to-end sandbox test (also run in CI)
│   ├── .generated/               written by initialize.sh (not tracked)
│   ├── project-template/         template for new project layers
│   └── project  ──symlink──►     SLOT: the active project layer (not tracked)
├── .github/workflows/ci.yml      CI: shellcheck, cred-gw tests, sandbox test
├── .dockerignore                 safety net for the build context (excludes everything)
├── .gitignore                    ignores the slot and .generated/
├── AGENTS.md                     workspace-wide AI instructions (all agents)
├── CLAUDE.md                     imports AGENTS.md for Claude
├── README.md
│
├── <working-repo-a>/             ┐ working repos: own git history, own AI instructions,
└── <working-repo-b>/             ┘ NOT tracked here (ignored via .git/info/exclude)
```

## Three layers

| | Base (this repo) | Agent modules (this repo, `modules/<name>/`) | Project layer (slot `.devcontainer/project`) |
|---|---|---|---|
| Image | `base/Dockerfile` → `ai-envelope-base:latest`: Node, CLI tools, uv, `dev` user, exclusion check | `Dockerfile` per module, stacked: install the CLI behind a wrapper that runs the exclusion check | `Dockerfile` `FROM` the last module image: system libs, CA certs, Python version, dependencies, pre-commit hooks |
| Compose | sandbox/egress networks, proxy, capabilities, proxy env, `~/.gitconfig` mount | `mounts` (agent home, token), `compose.yml` (own services, e.g. `cred-gw`), `configure` (hosts, networks) | compose `name`, masking mounts, build secrets, env vars |
| Selection | always | `modules` file in the project layer (default: `claude`) | the slot symlink |
| Build context filter | `.dockerignore` (excludes everything) | the module folder | `Dockerfile.dockerignore` (allowlist of files to `COPY`) |
| Secret masking | `check-exclusions` (the mechanism) | | `excluded-files` + `dummies/` (the paths) |
| Host protection | `protected-paths` (`.claude/`, `.vscode/`, …) + `.git/config`/`hooks` | | `protected-paths` (additions, `!` removals) |
| Claude settings | | `claude/managed-settings.json` (generic: Stop hook, `.env` read block) | `claude-settings.json` (optional: deny rules, hooks, attribution) |
| Egress | `proxy/allowlist.txt` (VS Code) | `allowlist.txt` (agent API, e.g. Anthropic, Copilot) | `allowlist.txt` (extra domains) |
| Host prerequisites | `~/.gitconfig` | `prereqs` (e.g. `~/.claude-devcontainer`, Copilot token) | `prereqs` (required repos, build secrets) |

How they are joined:

- `initialize.sh` runs on the host before every start (cached, so usually
  instant). It builds the base image, stacks the enabled modules on top
  (`ai-envelope-mod-claude-copilot:latest`) and generates
  `.generated/compose.yml` with the module mounts, the resulting image name and
  read-only mounts for every working repo's `.git/config` + `.git/hooks`.
- `devcontainer.json` loads `docker-compose.yml`, `.generated/compose.yml` and
  `project/compose.yml`. Compose merges them, so each file only adds to the
  previous ones.
- The devcontainer service always builds `project/Dockerfile`, which starts
  `FROM` the last module image (passed as `BASE_IMAGE`).
- The proxy reads `proxy/allowlist.txt`, the merged module allowlists and
  `project/allowlist.txt`.
- `check-exclusions` reads `project/excluded-files` and `project/dummies/`.

Details on modules (contents, adding one, the Copilot token):
[`.devcontainer/modules/README.md`](.devcontainer/modules/README.md).

The slot `.devcontainer/project` is a symlink (or a plain copy) and is ignored
by git. Without it, `initialize.sh` refuses to start. That way you can't
accidentally run the sandbox without the project's secret masks.

The sandbox itself (how it works, debugging, security notes) is described in
[`.devcontainer/README.md`](.devcontainer/README.md).

## Quick start (existing project layer)

1. Clone this repo, then clone the working repos into its root and ignore them
   (see [Ignoring working repos](#ignoring-working-repos)):
   ```bash
   git clone <envelope-url> workspace && cd workspace
   git clone <working-repo-url> <working-repo>
   echo '/<working-repo>/' >> .git/info/exclude
   ```
2. Link the project layer into the slot. The link target is relative to
   `.devcontainer/`:
   ```bash
   ln -s ../<umbrella-repo>/ai-envelope .devcontainer/project   # layer lives in a working repo
   ```
3. Create the host prerequisites: the generic ones in
   [`.devcontainer/README.md`](.devcontainer/README.md#prerequisites-host), plus
   anything the project layer's README lists.
4. Open the folder in VS Code and run **Dev Containers: Reopen in Container**,
   or use the devcontainer CLI:
   ```bash
   devcontainer up --workspace-folder .
   devcontainer exec --workspace-folder . zsh
   ```
5. Inside the container, run `claude` (or `copilot`, if the layer enables it).

## Starting a new project

The template [`.devcontainer/project-template/`](.devcontainer/project-template/)
is a complete, working project layer that adds nothing. Every file explains
what goes into it with commented examples. Copy it and fill it in step by step.

### 1. Set up the workspace

```bash
git clone <envelope-url> my-workspace && cd my-workspace
git clone <working-repo-url> my-repo
echo '/my-repo/' >> .git/info/exclude
git status                                  # my-repo must not show up
```

### 2. Create the project layer

Put the layer where it is versioned with the project, not in this repo.
A working repo is a good place, e.g. an umbrella/meta repo of the project:

```bash
cp -r .devcontainer/project-template my-repo/ai-envelope
ln -s ../my-repo/ai-envelope .devcontainer/project
```

(Just trying something out? `ln -s project-template .devcontainer/project`
gives you the plain generic sandbox with Claude Code.)

### 2b. Pick the agents (`modules`)

List the agent modules the project needs in `modules`, e.g. `claude` and
`copilot`. Each one has its own host prerequisites (`initialize.sh` tells you
what's missing). For Copilot, create the token as described in
[`.devcontainer/modules/README.md`](.devcontainer/modules/README.md#copilot-token).

### 3. Name the compose project

In `compose.yml`, set `name:` (e.g. `ai-envelope-myproject`). The containers
are then called `ai-envelope-myproject-devcontainer-1` and `…-proxy-1`, and
several projects can run side by side.

### 4. Declare required repos and host files (`prereqs`)

List every working repo whose secrets you mask (step 5), e.g.
`dir my-repo/.git`. A repo cloned under another name would otherwise run with
its secrets unmasked, and `check-exclusions` can't tell. Host files the build
needs (e.g. `~/.build-secrets/username`) go here too.

You don't need to protect the layer itself: if it lives outside
`.devcontainer/` (e.g. in `my-repo/`), `initialize.sh` mounts it read-only, so
the agent can't remove a mask or change the Dockerfile for the next rebuild.

### 5. Mask secrets

Find everything in the working repos the agent must not see: `.env` files,
config folders with credentials, build secrets, key files. For each:

1. Add the path (relative to `/workspace`) to `excluded-files`.
2. Add a read-only mount in `compose.yml`:
   ```yaml
   - /dev/null:/workspace/my-repo/.env:ro                              # blank a file
   - ./project/dummies/my-repo/config:/workspace/my-repo/config:ro    # replace with a dummy
   ```
   Paths in `compose.yml` are relative to `.devcontainer/`, so files of the layer
   are always addressed as `./project/...`.
3. For a dummy, create `dummies/<same path>` with **placeholder values only**.
   The app should still find the files it expects, just without real secrets.

Prefer masking whole directories over single files.

### 6. Add the toolchain (`Dockerfile`)

Install what the project needs on top of the module images: system packages,
company CA certificates, a Python version and dependencies via `uv`, pre-commit
hooks, and so on. The template `Dockerfile` has commented examples for each.

- Every file you `COPY` must be allowlisted in `Dockerfile.dockerignore`. The
  build context is the whole workspace, so never allowlist secret files.
- Install environments **outside** `/workspace` (e.g. `/home/dev/.venv`).
  `/workspace` is bind-mounted over at runtime.
- Private package indexes and git need credentials during the build: declare
  BuildKit secrets in `compose.yml` and use them with
  `RUN --mount=type=secret,...`. They never end up in the image. The
  template's `compose.yml` and `Dockerfile` show commented examples.

### 7. Allow extra domains (`allowlist.txt`)

Only if tooling inside the container really needs them, e.g. a package index
for `uv add` at runtime. The build itself does not go through the proxy.
Every domain is a possible exfiltration channel, so keep the list short. The
file must exist, even if it only has comments.

### 8. Add project AI instructions

Put repo-specific rules into the working repos themselves (`AGENTS.md`,
`CLAUDE.md`, …), not into the envelope. [`AGENTS.md`](AGENTS.md) tells the
agent to follow them.

### 9. Start and verify

```bash
devcontainer up --workspace-folder .
```

- `initialize.sh` shows which layer is active.
- `check-exclusions` must print `OK` for every masked path.
- `.devcontainer/compose.sh logs -f proxy` shows blocked requests
  (`TCP_DENIED`) if something can't reach the network.

### 10. Commit the layer

Commit the layer in its own repo (e.g. `my-repo`), following that repo's
rules. Never commit it to the envelope. Write down any extra host prerequisites
(e.g. `~/.build-secrets/`) in the layer's `README.md`.

## Ignoring working repos

Working repos are ignored through `.git/info/exclude`, not `.gitignore`. This
keeps the envelope free of project-specific names, but the file is local to
each clone, so set it up after cloning:

```bash
echo '/my-repo/' >> .git/info/exclude
git status    # the working repos must not show up
```

Run git commands from inside the respective directory: the envelope root for
setup changes, a working repo for project changes. Never commit project changes
to the envelope, or setup changes to a working repo.

## Security notes

What the sandbox protects against, and what not (VS Code host channels,
tokens readable by the agent, allowlisted domains as exfiltration channels),
is listed in
[`.devcontainer/README.md`](.devcontainer/README.md#security-notes--known-gaps).
