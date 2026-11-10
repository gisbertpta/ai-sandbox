# Project layer — image part. Starts from the last image of the module chain:
# generic base (zsh, git, rg, fd, jq, uv, user 'dev', exclusion check) plus the
# agent CLIs enabled in 'modules'. initialize.sh passes it as BASE_IMAGE.
# Build context: the workspace root. Only files allowlisted in
# Dockerfile.dockerignore are visible to COPY.
ARG BASE_IMAGE=ai-sandbox-base:latest
FROM ${BASE_IMAGE}

# Nothing project-specific yet. Typical additions (uncomment and adapt):

# ── System packages / company CA (needs root) ─────────────────────────────────
# USER root
# RUN apt-get update && apt-get install -y --no-install-recommends \
#         build-essential \
#     && rm -rf /var/lib/apt/lists/*
# COPY my-repo/company-ca.crt /usr/local/share/ca-certificates/company-ca.crt
# RUN update-ca-certificates
# USER dev

# ── Python toolchain + dependencies via uv ────────────────────────────────────
# Installs into /home/dev/.venv (outside /workspace, so the bind-mounted
# working repos don't hide it).
# ENV PATH="/home/dev/.venv/bin:${PATH}"
# ENV VIRTUAL_ENV="/home/dev/.venv"
# ENV UV_PROJECT_ENVIRONMENT="/home/dev/.venv"
# COPY --chown=dev:dev my-repo/pyproject.toml my-repo/uv.lock ./
# RUN uv python install 3.12 \
#     && uv sync --locked --python 3.12

# ── Private index / git with credentials (secrets declared in compose.yml) ────
# Secrets exist only during this RUN and never end up in the image.
# RUN --mount=type=secret,id=git-username,uid=1000 \
#     --mount=type=secret,id=git-password,uid=1000 \
#     git config --global credential.helper '!f() { echo "username=$(cat /run/secrets/git-username)"; echo "password=$(cat /run/secrets/git-password)"; }; f' && \
#     uv sync --locked && \
#     git config --global --unset credential.helper
