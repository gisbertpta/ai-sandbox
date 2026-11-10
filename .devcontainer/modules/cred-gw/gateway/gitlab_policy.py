"""GitLab rules: git over HTTPS and the REST API (glab), limited to listed projects and branches.

Git (smart HTTP):
- fetch/clone (info/refs?service=git-upload-pack, git-upload-pack): project in the read or write list
- push (info/refs?service=git-receive-pack, git-receive-pack): project in the write list; every pushed ref matches
  the branch pattern; no deletions, no tags

REST API (/api/v4):
- GET user, version, metadata
- GET users?username=<name>: one exact name (page, per_page optional); glab resolves MR reviewers this way
- GET projects/<project>[/...]: project in the read or write list; settings with secrets are refused
- POST projects/<project>/merge_requests: project (and target_project_id) in the write list
- PUT projects/<project>/merge_requests/<iid>, POST .../merge_requests/<iid>/notes|discussions: write list
- never: merge, approve, delete, other projects, groups, user lists or searches, search, GraphQL

Force pushes to an allowed branch cannot be told apart from normal pushes here; protect develop/main in GitLab.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core import Denied, Upstream

API = '/api/v4'
GIT_PATH = re.compile(
    r'^/(?P<project>[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+)/(?P<op>info/refs|git-upload-pack|git-receive-pack)$'
)
PROJECT_PATH = re.compile(rf'^{API}/projects/(?P<ref>[^/]+)(?P<rest>/.*)?$')
MR_WRITES = (
    ('POST', re.compile(r'^/merge_requests$')),
    ('PUT', re.compile(r'^/merge_requests/\d+$')),
    ('POST', re.compile(r'^/merge_requests/\d+/(notes|discussions)$')),
)
SECRET_SETTINGS = re.compile(r'^/(variables|access_tokens|deploy_tokens|deploy_keys|hooks|integrations|services)(/|$)')
ZERO_OID = re.compile(r'^0+$')
USERNAME = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.\-]*$')
USER_LOOKUP_PAGING = frozenset({'page', 'per_page'})
MAX_JSON_BYTES = 1_000_000


@dataclass(frozen=True)
class GitLabPolicy:
    """Projects (lowercase full paths) the agent may read or write, and the branch refs it may push."""

    read_projects: frozenset[str]
    write_projects: frozenset[str]
    push_refs: re.Pattern[str]

    def access(self, project: str) -> str:
        """Return 'write', 'read' or '' for a project path."""
        name = project.lower().removesuffix('.git')
        if name in self.write_projects:
            return 'write'
        return 'read' if name in self.read_projects else ''


@dataclass(frozen=True)
class Plan:
    """An allowed request: projects (path or numeric id) and the access each needs; the query to forward."""

    needs: tuple[tuple[str, str], ...]
    query: str


def plan_request(policy: GitLabPolicy, method: str, path: str, query: str, content_type: str, body: bytes | None) -> Plan:
    """Decide whether a request may pass and which project access it needs.

    Raises:
        Denied: when no rule allows the request.
    """
    lowered = path.lower()
    if '..' in path or '//' in path or '%2e' in lowered or '%5c' in lowered or '\\' in path:
        raise Denied(400, 'path not allowed')
    git = GIT_PATH.match(path)
    if git:
        return plan_git(policy, method, git['project'].removesuffix('.git'), git['op'], query, body)
    if method == 'GET' and path in (f'{API}/user', f'{API}/version', f'{API}/metadata'):
        return Plan((), query)
    if method == 'GET' and path == f'{API}/users':
        return Plan((), user_lookup_query(query))
    project = PROJECT_PATH.match(path)
    if project:
        ref = urllib.parse.unquote(project['ref'])
        rest = project['rest'] or ''
        if '%' in rest:
            raise Denied(400, 'encoded characters after the project are not allowed')
        return plan_api(method, ref, rest, query, content_type, body)
    raise Denied(403, f'{method} {path} is not allowed by cred-gw')


def user_lookup_query(query: str) -> str:
    """Allow the lookup of one user by exact username, as glab does for `--reviewer`; no list, no search.

    Raises:
        Denied: without exactly one valid `username`, or with any parameter besides page and per_page.
    """
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    names = params.get('username', [])
    if len(names) != 1 or not USERNAME.match(names[0]) or set(params) - {'username'} - USER_LOOKUP_PAGING:
        raise Denied(403, 'only GET users?username=<name> is allowed by cred-gw')
    return query


def plan_git(policy: GitLabPolicy, method: str, project: str, op: str, query: str, body: bytes | None) -> Plan:
    """Plan a git smart-HTTP request.

    Raises:
        Denied: for unknown services, wrong methods or refs outside the push pattern.
    """
    if op == 'info/refs':
        service = urllib.parse.parse_qs(query).get('service', [''])
        if method != 'GET' or service not in (['git-upload-pack'], ['git-receive-pack']):
            raise Denied(403, 'only smart HTTP is supported')
        return Plan(((project, 'write' if service == ['git-receive-pack'] else 'read'),), query)
    if method != 'POST':
        raise Denied(405, f'{op} needs POST')
    if op == 'git-upload-pack':
        return Plan(((project, 'read'),), query)
    commands, capabilities = pushed_refs(body or b'')
    for ref, new in commands:
        if ZERO_OID.match(new):
            raise push_rejected(commands, capabilities, f'deleting {ref} is not allowed')
        if not policy.push_refs.match(ref):
            raise push_rejected(commands, capabilities, f'pushing {ref} is not allowed')
    return Plan(((project, 'write'),), query)


def pkt_line(data: bytes) -> bytes:
    """Encode one pkt-line."""
    return f'{len(data) + 4:04x}'.encode() + data


def push_rejected(commands: list[tuple[str, str]], capabilities: set[str], reason: str) -> Denied:
    """Build a refusal that git shows per ref ('! [remote rejected] ... (cred-gw: reason)') via report-status."""
    report = pkt_line(b'unpack ok\n') + b''.join(pkt_line(f'ng {ref} cred-gw: {reason}\n'.encode()) for ref, _ in commands)
    report += b'0000'
    if capabilities & {'side-band', 'side-band-64k'}:
        report = pkt_line(b'\x01' + report) + b'0000'
    if not capabilities & {'report-status', 'report-status-v2'}:
        return Denied(403, reason)
    return Denied(403, reason, ('application/x-git-receive-pack-result', report))


def pushed_refs(body: bytes) -> tuple[list[tuple[str, str]], set[str]]:
    """Read the ref update commands (ref, new id) and the client capabilities at the start of a receive-pack request.

    Raises:
        Denied: when the request does not start with well-formed commands.
    """
    commands: list[tuple[str, str]] = []
    capabilities: set[str] = set()
    position = 0
    while True:
        try:
            length = int(body[position : position + 4], 16)
        except ValueError as error:
            raise Denied(400, 'malformed push request') from error
        if length == 0:
            break
        if length < 4 or position + length > len(body):
            raise Denied(400, 'malformed push request')
        line, _, caps = body[position + 4 : position + length].rstrip(b'\n').decode('utf-8', 'replace').partition('\0')
        capabilities.update(caps.split())
        position += length
        if line.startswith('shallow '):
            continue
        parts = line.split(' ')
        if len(parts) != 3:
            raise Denied(400, 'malformed push command')
        commands.append((parts[2], parts[1]))
    if not commands:
        raise Denied(400, 'push without ref updates')
    return commands, capabilities


def plan_api(method: str, ref: str, rest: str, query: str, content_type: str, body: bytes | None) -> Plan:
    """Plan a REST request below projects/<ref>.

    Raises:
        Denied: for secret settings, writes outside merge requests and target projects outside the write list.
    """
    if method == 'GET':
        if SECRET_SETTINGS.match(rest):
            raise Denied(403, f'projects/*{rest} is not allowed')
        return Plan(((ref, 'read'),), query)
    if not any(method == allowed and pattern.match(rest) for allowed, pattern in MR_WRITES):
        raise Denied(403, f'{method} projects/*{rest} is not allowed')
    needs: list[tuple[str, str]] = [(ref, 'write')]
    target = form_or_json(content_type, body).get('target_project_id')
    if target is not None:
        needs.append((str(target), 'write'))
    return Plan(tuple(needs), query)


def form_or_json(content_type: str, body: bytes | None) -> dict[str, Any]:
    """Decode a JSON or form-encoded request body into a dict (empty without body).

    Raises:
        Denied: when the body is too large, of another type or not an object.
    """
    if not body:
        return {}
    if len(body) > MAX_JSON_BYTES:
        raise Denied(413, 'request body too large')
    if content_type.startswith('application/x-www-form-urlencoded'):
        return {name: values[-1] for name, values in urllib.parse.parse_qs(body.decode('utf-8', 'replace')).items()}
    try:
        decoded = json.loads(body)
    except ValueError as error:
        raise Denied(400, 'request body is no valid JSON') from error
    if not isinstance(decoded, dict):
        raise Denied(400, 'JSON object body required')
    return decoded


@dataclass
class GitLabService:
    """GitLab behind the gateway; caches numeric project id -> path lookups."""

    host: str
    policy: GitLabPolicy
    project_paths: dict[str, str] = field(default_factory=dict)

    def authorize(
        self, method: str, path: str, query: str, headers: Mapping[str, str], body: bytes | None, upstream: Upstream
    ) -> str:
        """Apply the project and branch rules.

        Raises:
            Denied: when a rule refuses the request or a project is outside the lists.
        """
        plan = plan_request(self.policy, method, path, query, headers.get('Content-Type', ''), body)
        for ref, needed in plan.needs:
            project = self.project_path(ref, upstream) if ref.isdigit() else ref
            granted = self.policy.access(project)
            if not granted or (needed == 'write' and granted != 'write'):
                raise Denied(403, f'{needed} access to project {ref} is not allowed')
        return plan.query

    def project_path(self, project_id: str, upstream: Upstream) -> str:
        """Resolve a numeric project id to its full path (empty when unknown)."""
        if project_id not in self.project_paths:
            status, project = upstream.get_json(f'{API}/projects/{project_id}')
            found = project.get('path_with_namespace') if status == 200 and isinstance(project, dict) else None
            if not isinstance(found, str):
                return ''
            self.project_paths[project_id] = found
        return self.project_paths[project_id]

    def response_observer(self, method: str, path: str) -> None:
        """GitLab responses are not inspected."""
        return None


def gitlab_auth(token: str, git_user: str = 'oauth2') -> Any:
    """Return the credential function: Basic auth for git smart HTTP, PRIVATE-TOKEN for the API."""
    basic = 'Basic ' + base64.b64encode(f'{git_user}:{token}'.encode()).decode()

    def headers(path_query: str) -> dict[str, str]:
        if path_query.startswith(f'{API}/'):
            return {'PRIVATE-TOKEN': token}
        return {'Authorization': basic}

    return headers
