FROM python:3.11-slim

# uid 1000 = the host user, so the 0600 token and key files from the host are readable
RUN useradd --uid 1000 --no-create-home gw
COPY core.py jira_policy.py gitlab_policy.py server.py /app/
USER gw
EXPOSE 443
CMD ["python3", "-u", "/app/server.py"]
