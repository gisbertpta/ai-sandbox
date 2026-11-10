# Pinned (tag + digest); .github/dependabot.yml proposes updates. CI runs the
# policy tests in this image too.
FROM python:3.11.18-slim-trixie@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

# uid 1000 = the host user, so the 0600 token and key files from the host are readable
RUN useradd --uid 1000 --no-create-home gw
COPY core.py jira_policy.py gitlab_policy.py server.py /app/
USER gw
EXPOSE 443
CMD ["python3", "-u", "/app/server.py"]
