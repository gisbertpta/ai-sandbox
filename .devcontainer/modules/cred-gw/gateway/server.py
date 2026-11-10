"""Credential gateway for the AI sandbox: holds the Jira and GitLab tokens and enforces rules the agent cannot change.

Inside the devcontainer the real host names resolve to this gateway (extra_hosts), which serves HTTPS with a
certificate from the sandbox CA, routes by Host header, applies the service's rules (jira_policy, gitlab_policy),
adds the credentials and forwards to the real host. Logs hold host, method, path (no query), decision and status;
never bodies.

Environment (a service is on when its UPSTREAM is set):
    JIRA_GW_UPSTREAM        https://jira.example.com
    JIRA_GW_LABEL           label that marks the project's issues
    JIRA_GW_PROJECTS        comma-separated project keys whose metadata (components, versions, boards) may be read
    JIRA_GW_ALLOW_WRITES    '1' enables edit, comment, transition and create (default: read-only)
    JIRA_GW_TOKEN_FILE      default /run/secrets/jira-token
    GITLAB_GW_UPSTREAM      https://gitlab.example.com
    GITLAB_GW_READ          comma-separated project paths the agent may read (fetch, API GET)
    GITLAB_GW_WRITE         comma-separated project paths the agent may push to and open merge requests in
    GITLAB_GW_PUSH_REFS     regex for pushable refs (default: ^refs/heads/(feature|fix|bugfix|chore|docs|refactor|test)/)
    GITLAB_GW_GIT_USER      user name for git Basic auth with the token (default oauth2)
    GITLAB_GW_TOKEN_FILE    default /run/secrets/gitlab-token
    CRED_GW_UPSTREAM_CA     optional CA bundle for the upstream TLS certificates
    CRED_GW_TLS_CERT        certificate chain served to the devcontainer (SANs: all upstream host names)
    CRED_GW_TLS_KEY         its key
    CRED_GW_PORT            listen port (default 443)
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from core import Denied, Service, Upstream
from gitlab_policy import GitLabPolicy, GitLabService, gitlab_auth
from jira_policy import JiraService, Policy, bearer

logger = logging.getLogger('cred-gw')

# shortcut: request bodies are buffered in memory up to this size — stream them if pushes ever get larger
MAX_BODY_BYTES = 256 * 1024 * 1024
FORWARD_REQUEST_HEADERS = ('Accept', 'Content-Type', 'Content-Encoding', 'Git-Protocol', 'User-Agent')
FORWARD_RESPONSE_HEADERS = (
    'Content-Type', 'Content-Encoding', 'Cache-Control', 'Expires', 'Pragma', 'Location', 'Link',
    'X-Total', 'X-Total-Pages', 'X-Page', 'X-Per-Page', 'X-Next-Page', 'X-Prev-Page',
)
CHUNK = 64 * 1024


class QuietServer(ThreadingHTTPServer):
    """Logs failed connections (TLS handshake, port probes) in one line instead of a traceback."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.info('connection from %s failed', client_address[0])


def make_handler(routes: dict[str, tuple[Service, Upstream]]) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to the routes (lowercase host name -> service and its upstream)."""

    class Handler(BaseHTTPRequestHandler):
        """Applies the service's rules to every request and streams the allowed ones."""

        def do_GET(self) -> None:
            self.handle_any()

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_GET

        def handle_any(self) -> None:
            host = (self.headers.get('Host') or '').split(':')[0].lower()
            path, _, query = self.path.partition('?')
            try:
                if host not in routes:
                    raise Denied(421, f'unknown host {host!r}')
                service, upstream = routes[host]
                body = self.read_body()
                forward_query = service.authorize(self.command, path, query, self.headers, body, upstream)
                headers = {name: self.headers[name] for name in FORWARD_REQUEST_HEADERS if self.headers.get(name)}
                response = upstream.open(self.command, path + (f'?{forward_query}' if forward_query else ''), body, headers)
                observer = service.response_observer(self.command, path)
            except Denied as denied:
                logger.info('deny %s %s %s: %s', host, self.command, path, denied.reason)
                # 'message' is read by glab, 'errorMessages' by the Jira scripts; git gets its own report format
                text = f'cred-gw: {denied.reason}'
                content_type, message = denied.reply or ('application/json', json.dumps({'message': text, 'errorMessages': [text]}).encode())
                self.send_response(200 if denied.reply else denied.status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(message)))
                self.end_headers()
                self.wfile.write(message)
                return
            except OSError as error:
                logger.warning('upstream error %s %s %s: %s', host, self.command, path, type(error).__name__)
                self.send_error(502, 'cred-gw: upstream unreachable')
                return
            with response:
                logger.info('pass %s %s %s -> %d', host, self.command, path, response.status)
                self.stream(response, observer if response.status == 200 else None)

        def read_body(self) -> bytes | None:
            """Read the request body (Content-Length or chunked).

            Raises:
                Denied: when the body exceeds MAX_BODY_BYTES or the chunking is malformed.
            """
            if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
                return self.read_chunked()
            length = int(self.headers.get('Content-Length') or 0)
            if length > MAX_BODY_BYTES:
                raise Denied(413, 'request body too large')
            return self.rfile.read(length) if length else None

        def read_chunked(self) -> bytes:
            parts: list[bytes] = []
            total = 0
            while True:
                try:
                    size = int(self.rfile.readline(1024).split(b';')[0].strip(), 16)
                except ValueError as error:
                    raise Denied(400, 'malformed chunked body') from error
                if size == 0:
                    while self.rfile.readline(1024).strip():  # trailers
                        pass
                    return b''.join(parts)
                total += size
                if total > MAX_BODY_BYTES:
                    raise Denied(413, 'request body too large')
                parts.append(self.rfile.read(size))
                self.rfile.readline(1024)

        def stream(self, response: Any, observer: Callable[[bytes], None] | None) -> None:
            """Copy status, selected headers and the body; the connection closes afterwards (HTTP/1.0).

            An observer gets the whole body before it is sent, so that body is buffered instead of streamed.
            """
            self.send_response(response.status)
            for name in FORWARD_RESPONSE_HEADERS:
                if response.headers.get(name):
                    self.send_header(name, response.headers[name])
            if response.headers.get('Content-Length'):
                self.send_header('Content-Length', response.headers['Content-Length'])
            self.end_headers()
            if self.command == 'HEAD':
                return
            if observer:
                content = response.read()
                observer(content)
                self.wfile.write(content)
                return
            while chunk := response.read(CHUNK):
                self.wfile.write(chunk)

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default access log; it would include query strings (JQL)."""
        
        def finish(self) -> None:
            """Send TLS close_notify; GnuTLS clients (git) reject a close-delimited body without it."""
            super().finish()
            try:
                self.connection.settimeout(5)
                self.connection.unwrap()
            except (OSError, ValueError):
                pass
    return Handler


def read_secret(variable: str, default: str) -> str:
    """Read a token file named by an environment variable."""
    with open(os.environ.get(variable, default), encoding='utf-8') as file:
        token = file.read().strip()
    if not token:
        raise SystemExit(f'{variable}: token file is empty')
    return token


def https_host(variable: str) -> str:
    """Return the host name of an https:// upstream URL from the environment."""
    url = os.environ[variable]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname:
        raise SystemExit(f'{variable} must be an https:// URL')
    return parts.hostname.lower()


def project_list(variable: str) -> frozenset[str]:
    """Parse a comma-separated list of project paths (lowercase, without .git)."""
    raw = os.environ.get(variable, '')
    return frozenset(item.strip().lower().removesuffix('.git') for item in raw.split(',') if item.strip())


def build_routes(context: ssl.SSLContext) -> dict[str, tuple[Service, Upstream]]:
    """Build the services configured in the environment."""
    routes: dict[str, tuple[Service, Upstream]] = {}
    if os.environ.get('JIRA_GW_UPSTREAM'):
        host = https_host('JIRA_GW_UPSTREAM')
        label = os.environ['JIRA_GW_LABEL'].strip()
        if not label or '"' in label or '\\' in label:
            raise SystemExit('JIRA_GW_LABEL must be set and free of quotes and backslashes')
        token = read_secret('JIRA_GW_TOKEN_FILE', '/run/secrets/jira-token')
        projects = frozenset(item.strip().upper() for item in os.environ.get('JIRA_GW_PROJECTS', '').split(',') if item.strip())
        if not all(re.fullmatch(r'[A-Z][A-Z0-9_]+', project) for project in projects):
            raise SystemExit('JIRA_GW_PROJECTS must be comma-separated Jira project keys')
        policy = Policy(label=label, projects=projects, allow_writes=os.environ.get('JIRA_GW_ALLOW_WRITES') == '1')
        auth: Callable[[str], dict[str, str]] = lambda _path, token=token: bearer(token)
        routes[host] = (JiraService(host, policy), Upstream(os.environ['JIRA_GW_UPSTREAM'], context, auth))
        logger.info(
            'jira %s: label %r, projects %s, writes %s',
            host, label, ','.join(sorted(projects)) or '-', 'on' if policy.allow_writes else 'off',
        )
    if os.environ.get('GITLAB_GW_UPSTREAM'):
        host = https_host('GITLAB_GW_UPSTREAM')
        write = project_list('GITLAB_GW_WRITE')
        policy_gl = GitLabPolicy(
            read_projects=project_list('GITLAB_GW_READ') | write,
            write_projects=write,
            push_refs=re.compile(
                os.environ.get('GITLAB_GW_PUSH_REFS') or r'^refs/heads/(feature|fix|bugfix|chore|docs|refactor|test)/'
            ),
        )
        token = read_secret('GITLAB_GW_TOKEN_FILE', '/run/secrets/gitlab-token')
        upstream = Upstream(os.environ['GITLAB_GW_UPSTREAM'], context, gitlab_auth(token, os.environ.get('GITLAB_GW_GIT_USER') or 'oauth2'))
        routes[host] = (GitLabService(host, policy_gl), upstream)
        logger.info('gitlab %s: read %d, write %d projects', host, len(policy_gl.read_projects), len(write))
    if not routes:
        raise SystemExit('no service configured (JIRA_GW_UPSTREAM, GITLAB_GW_UPSTREAM)')
    return routes


def main() -> None:
    """Read the environment, build the services and serve HTTPS."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    context = ssl.create_default_context()
    if os.environ.get('CRED_GW_UPSTREAM_CA'):
        context.load_verify_locations(cafile=os.environ['CRED_GW_UPSTREAM_CA'])
    server = QuietServer(('0.0.0.0', int(os.environ.get('CRED_GW_PORT') or 443)), make_handler(build_routes(context)))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(os.environ['CRED_GW_TLS_CERT'], os.environ['CRED_GW_TLS_KEY'])
    # Handshake in the request thread, so a stalled client cannot block accept()
    server.socket = server_context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    server.serve_forever()


if __name__ == '__main__':
    main()
