# Module: Claude Code. Stacked on the previous image of the module chain
# (BASE_IMAGE is passed by initialize.sh). Build context: this folder.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
RUN npm install -g @anthropic-ai/claude-code@latest \
    && mv "$(command -v claude)" /usr/local/bin/claude.real

# Wrapper: exclusion check, then Claude in bypass-permissions mode
COPY --chown=root:root claude-wrapper.sh /usr/local/bin/claude

# Managed settings: highest precedence, apply in every directory (also nested
# repos), and root-owned, so the agent can't change them
COPY --chown=root:root managed-settings.json /etc/claude-code/managed-settings.json
COPY --chown=root:root notify-ntfy.sh /usr/local/libexec/ai-envelope/notify-ntfy.sh
RUN chmod 755 /usr/local/bin/claude /usr/local/libexec/ai-envelope/notify-ntfy.sh \
    && chmod 644 /etc/claude-code/managed-settings.json \
    && install -d -m 755 /etc/claude-code/managed-settings.d
# managed-settings.d/: the project layer's claude-settings.json is mounted here
# read-only (by initialize.sh) and merged over managed-settings.json

# Claude Code starts its shells with NO_PROXY=localhost while no_proxy keeps the
# full list; Go tools (glab) read NO_PROXY first and would send hosts that bypass
# the proxy (e.g. via a credential gateway) to the proxy instead
RUN printf '%s\n' '[ -n "${no_proxy:-}" ] && export NO_PROXY="$no_proxy"' >> /etc/zsh/zshenv

# Updates come with an image rebuild (NO_CACHE=1 initialize.sh), not at runtime
ENV DISABLE_AUTOUPDATER=1
# Load CLAUDE.md (and its @AGENTS.md import) of the nested repos the wrapper
# adds via --add-dir, so their rules apply when Claude starts in /workspace
ENV CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1
USER dev
