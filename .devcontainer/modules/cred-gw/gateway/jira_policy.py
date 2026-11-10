"""Jira Data Center rules: every request stays inside one label on a shared board.

Reads (GET):
- /rest/api/2/issue/{key}[/<sub>]                    the issue must carry the label (sub: one word, e.g. comment,
                                                      transitions, worklog, remotelink, watchers, votes, editmeta)
- /rest/api/2/search?jql=...                         JQL is wrapped: (<jql>) AND labels = "<label>"
- /rest/agile/1.0/{board,sprint,epic}/.../issue      JQL is wrapped the same way (also without a jql parameter)
- metadata without issue content                     fixed allowlist (myself, field, status, issuetype, ...); project
                                                      endpoints and board listings only for the configured projects
- /secure/attachment/{id}/..., /secure/thumbnail/...,  only attachments the gateway saw in a labelled issue it passed
  /rest/api/2/attachment/{id}                          before (GET issue, search, agile issue lists)
With writes enabled also:
- PUT  /rest/api/2/issue/{key}                        issue must carry the label; the edit must keep it
- POST /rest/api/2/issue/{key}/comment                issue must carry the label
- POST /rest/api/2/issue/{key}/transitions            issue must carry the label; field updates must keep it
- POST /rest/api/2/issue                              new issue must carry the label; a parent must carry it too

Everything else is refused, among it filters, dashboards, issue picker, JQL autocomplete and user search: they
expose issues or people outside the label.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core import Denied, Upstream

API = '/rest/api/2'
AGILE = '/rest/agile/1.0'
ISSUE_REF = r'(?:[A-Z][A-Z0-9_]+-\d+|\d+)'
PROJECT_KEY = r'(?P<project>[A-Za-z][A-Za-z0-9_]+)'
ISSUE_PATH = re.compile(rf'^{API}/issue/(?P<key>{ISSUE_REF})(?P<sub>/[A-Za-z]+)?$')
SEARCH_PATHS = (
    re.compile(rf'^{AGILE}/board/\d+/(?:issue|backlog|epic/(?:\d+|none)/issue|sprint/\d+/issue)$'),
    re.compile(rf'^{AGILE}/sprint/\d+/issue$'),
    re.compile(rf'^{AGILE}/epic/{ISSUE_REF}/issue$'),
)
METADATA_PATHS = (
    re.compile(rf'^{API}/(?:myself|serverInfo|field|statuscategory|priority|resolution|issueLinkType|user)$'),
    re.compile(rf'^{API}/(?:status|issuetype)(?:/\d+)?$'),
    re.compile(rf'^{AGILE}/board/\d+(?:/configuration|/sprint)?$'),
    re.compile(rf'^{AGILE}/sprint/\d+$'),
)
PROJECT_PATHS = (
    re.compile(rf'^{API}/project/{PROJECT_KEY}(?:/components|/versions|/statuses)?$'),
    re.compile(rf'^{API}/issue/createmeta/{PROJECT_KEY}/issuetypes(?:/\d+)?$'),
)
ATTACHMENT_PATHS = (
    re.compile(r'^/secure/(?:attachment|thumbnail)/(?P<id>\d+)/(?!\.\.?$)[^/]+$'),
    re.compile(rf'^{API}/attachment/(?P<id>\d+)$'),
)
MAX_BODY_BYTES = 1_000_000


@dataclass(frozen=True)
class Policy:
    """The gateway's rules: the label that scopes every request, the projects whose metadata may be read and whether
    writes are allowed."""

    label: str
    projects: frozenset[str]
    allow_writes: bool


@dataclass(frozen=True)
class Plan:
    """What to do with an allowed request.

    Attributes:
        check_keys: issues that must carry the label before the request is forwarded.
        query: the query string to forward (rewritten for search).
        attachment: attachment id that must have been seen in a labelled issue (empty when none).
    """

    check_keys: tuple[str, ...]
    query: str
    attachment: str = ''


def plan_request(policy: Policy, method: str, path: str, query: str, body: Any) -> Plan:
    """Decide whether a request may pass and which label checks it needs.

    Raises:
        Denied: when no rule allows the request.
    """
    if method == 'GET':
        return plan_read(policy, path, query)
    match = ISSUE_PATH.match(path)
    if method in ('PUT', 'POST') and not policy.allow_writes:
        raise Denied(403, 'jira-gw is read-only')
    if method == 'PUT' and match and not match['sub']:
        require_label_kept(body, policy.label)
        return Plan((match['key'],), query)
    if method == 'POST' and match and match['sub'] == '/comment':
        return Plan((match['key'],), query)
    if method == 'POST' and match and match['sub'] == '/transitions':
        require_label_kept(body, policy.label)
        return Plan((match['key'],), query)
    if method == 'POST' and path == f'{API}/issue':
        return Plan(new_issue_checks(body, policy.label), query)
    raise Denied(403, f'{method} {path} is not allowed by jira-gw')


def plan_read(policy: Policy, path: str, query: str) -> Plan:
    """Decide a GET request: issue reads, scoped searches, metadata and seen attachments.

    Raises:
        Denied: when no read rule allows the request.
    """
    if path == f'{API}/search':
        return Plan((), scoped_search_query(query, policy.label))
    if any(pattern.match(path) for pattern in SEARCH_PATHS):
        return Plan((), scoped_search_query(query, policy.label, required=False))
    if match := ISSUE_PATH.match(path):
        return Plan((match['key'],), query)
    if any(pattern.match(path) for pattern in METADATA_PATHS):
        return Plan((), query)
    if path == f'{AGILE}/board':
        require_own_project(policy, dict(urllib.parse.parse_qsl(query)).get('projectKeyOrId', ''))
        return Plan((), query)
    for pattern in PROJECT_PATHS:
        if match := pattern.match(path):
            require_own_project(policy, match['project'])
            return Plan((), query)
    for pattern in ATTACHMENT_PATHS:
        if match := pattern.match(path):
            return Plan((), query, attachment=match['id'])
    raise Denied(403, f'GET {path} is not allowed by jira-gw')


def require_own_project(policy: Policy, project: str) -> None:
    """Refuse a project outside the configured ones (keys compare case-insensitively, like Jira).

    Raises:
        Denied: when the project is not configured.
    """
    if project.upper() not in policy.projects:
        raise Denied(403, f'project {project!r} is not allowed by jira-gw')


def scoped_search_query(query: str, label: str, required: bool = True) -> str:
    """Return the query string with the JQL restricted to the label.

    Args:
        query: the request's query string.
        label: the label every result must carry.
        required: whether the request must bring a JQL condition (search); agile issue lists may omit it.

    Raises:
        Denied: when the JQL is missing although required or could escape the wrapping parentheses.
    """
    params = urllib.parse.parse_qsl(query, keep_blank_values=True)
    jql_values = [value for name, value in params if name == 'jql']
    if len(jql_values) > 1 or (required and not jql_values):
        raise Denied(400, 'search needs exactly one jql parameter')
    condition, order_by = split_order_by(jql_values[0]) if jql_values else ('', '')
    if not condition.strip() and (required or order_by):
        raise Denied(400, 'search needs a JQL condition')
    scoped = f'({condition}) AND labels = "{label}"{order_by}' if condition.strip() else f'labels = "{label}"'
    rest = [(name, value) for name, value in params if name != 'jql']
    return urllib.parse.urlencode([('jql', scoped), *rest])


def split_order_by(jql: str) -> tuple[str, str]:
    """Split JQL into condition and a trailing ' ORDER BY ...' clause, checking quotes and parentheses.

    Raises:
        Denied: on unbalanced parentheses or quotes, or a backslash outside quotes; each could break out of the
            wrapping parentheses.
    """
    depth = 0
    quote = ''
    escaped = False
    order_at = -1
    for index, char in enumerate(jql):
        if escaped:
            escaped = False
        elif quote:
            if char == '\\':
                escaped = True
            elif char == quote:
                quote = ''
        elif char == '\\':
            raise Denied(400, 'backslash outside a quoted JQL string is not allowed')
        elif char in '"\'':
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                raise Denied(400, 'unbalanced parentheses in JQL')
        elif depth == 0 and order_at < 0 and re.match(r'(?i)\border\s+by\b', jql[index:]):
            if index == 0 or not (jql[index - 1].isalnum() or jql[index - 1] == '_'):
                order_at = index
    if quote or escaped or depth:
        raise Denied(400, 'unbalanced quotes or parentheses in JQL')
    if order_at < 0:
        return jql, ''
    return jql[:order_at], ' ' + jql[order_at:].strip()


def require_label_kept(body: Any, label: str) -> None:
    """Refuse an edit that could remove the label.

    Raises:
        Denied: when the body is no JSON object, uses `update.labels`, or sets `fields.labels` without the label.
    """
    if not isinstance(body, dict):
        raise Denied(400, 'JSON object body required')
    update = body.get('update')
    if isinstance(update, dict) and 'labels' in update:
        raise Denied(403, 'update.labels is not allowed; set fields.labels including the project label')
    fields = body.get('fields')
    if isinstance(fields, dict) and 'labels' in fields and not has_label(fields['labels'], label):
        raise Denied(403, f'fields.labels must keep {label!r}')


def new_issue_checks(body: Any, label: str) -> tuple[str, ...]:
    """Check a create body; return the parent key whose label must be checked, if any.

    Raises:
        Denied: when the new issue would not carry the label.
    """
    require_label_kept(body, label)
    fields = body.get('fields')
    if not isinstance(fields, dict) or not has_label(fields.get('labels'), label):
        raise Denied(403, f'a new issue must carry the label {label!r} in fields.labels')
    parent = fields.get('parent')
    if parent is None:
        return ()
    ref = (parent.get('key') or parent.get('id')) if isinstance(parent, dict) else None
    if not isinstance(ref, str) or not re.fullmatch(ISSUE_REF, ref):
        raise Denied(400, 'fields.parent needs a key or id')
    return (ref,)


def has_label(raw: Any, label: str) -> bool:
    """True when a labels value (list of strings) contains the label, ignoring case like the agent scripts."""
    return isinstance(raw, list) and label.casefold() in {str(item).strip().casefold() for item in raw}


def parse_body(raw: bytes | None) -> Any:
    """Decode a JSON request body; None when there is none.

    Raises:
        Denied: when the body is too large or no valid JSON.
    """
    if raw is None:
        return None
    if len(raw) > MAX_BODY_BYTES:
        raise Denied(413, 'request body too large')
    try:
        return json.loads(raw)
    except ValueError as error:
        raise Denied(400, 'request body is no valid JSON') from error


def attachment_ids(payload: Any) -> set[str]:
    """Collect the attachment ids of an issue or of the issues of a search result (`fields.attachment[].id`)."""
    issues = payload.get('issues') if isinstance(payload, dict) and 'issues' in payload else [payload]
    found: set[str] = set()
    for issue in issues if isinstance(issues, list) else ():
        fields = issue.get('fields') if isinstance(issue, dict) else None
        attachments = fields.get('attachment') if isinstance(fields, dict) else None
        for attachment in attachments if isinstance(attachments, list) else ():
            if isinstance(attachment, dict) and str(attachment.get('id', '')).isdigit():
                found.add(str(attachment['id']))
    return found


def bearer(token: str) -> dict[str, str]:
    """Credential headers for Jira Data Center (personal access token)."""
    return {'Authorization': f'Bearer {token}'}


@dataclass(frozen=True)
class JiraService:
    """Jira behind the gateway; remembers the attachment ids of the labelled issues it passed."""

    host: str
    policy: Policy
    # shortcut: kept in memory for the gateway's lifetime — a restart forgets them, the issue must be read again
    attachments: set[str] = field(default_factory=set)

    def authorize(
        self, method: str, path: str, query: str, headers: Mapping[str, str], body: bytes | None, upstream: Upstream
    ) -> str:
        """Apply the label rules; look up the label of every issue the request touches.

        Raises:
            Denied: when a rule refuses the request, an issue does not carry the label or an attachment was not seen
                in a labelled issue.
        """
        plan = plan_request(self.policy, method, path, query, parse_body(body))
        if plan.attachment and plan.attachment not in self.attachments:
            raise Denied(403, f'attachment {plan.attachment} was not seen in a labelled issue; read its issue first')
        for key in plan.check_keys:
            status, issue = upstream.get_json(f'{API}/issue/{urllib.parse.quote(key)}?fields=labels')
            fields = issue.get('fields') if status == 200 and isinstance(issue, dict) else None
            if not isinstance(fields, dict) or not has_label(fields.get('labels'), self.policy.label):
                raise Denied(403, f'{key} does not carry the label {self.policy.label!r}')
        return plan.query

    def response_observer(self, method: str, path: str) -> Callable[[bytes], None] | None:
        """Return a callback that records attachment ids from the response of a labelled issue read or search."""
        match = ISSUE_PATH.match(path)
        issue_read = bool(match and not match['sub'])
        search = path == f'{API}/search' or any(pattern.match(path) for pattern in SEARCH_PATHS)
        return self.remember_attachments if method == 'GET' and (issue_read or search) else None

    def remember_attachments(self, body: bytes) -> None:
        """Record the attachment ids in a JSON response body; a body that is no JSON records nothing."""
        try:
            payload = json.loads(body)
        except ValueError:
            return  # e.g. a compressed body: its attachments stay unknown and their downloads are refused
        self.attachments.update(attachment_ids(payload))
