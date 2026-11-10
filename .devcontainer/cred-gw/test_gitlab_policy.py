"""Tests for the GitLab rules. Run in cred-gw/: python3 -m unittest -v (stdlib only)."""

from __future__ import annotations

import re
import unittest

from core import Denied
from gitlab_policy import API, GitLabPolicy, GitLabService, gitlab_auth, plan_request, pushed_refs

CODE = 'grp/sub/example-code'
LIB = 'grp/shared/tools'
POLICY = GitLabPolicy(
    read_projects=frozenset({LIB, CODE}),
    write_projects=frozenset({CODE}),
    push_refs=re.compile(r'^refs/heads/(feature|fix)/'),
)
OLD = 'a' * 40
NEW = 'b' * 40
ZERO = '0' * 40


def pkt(line: str) -> bytes:
    """Encode one pkt-line."""
    data = line.encode()
    return f'{len(data) + 4:04x}'.encode() + data


def push_body(*commands: tuple[str, str, str]) -> bytes:
    """Build a receive-pack request: commands, flush, then a fake pack."""
    lines = [f'{old} {new} {ref}' + ('\0report-status side-band-64k\n' if index == 0 else '\n')
             for index, (old, new, ref) in enumerate(commands)]
    return b''.join(pkt(line) for line in lines) + b'0000PACK\x00\x00\x00\x02'


def needs(method: str, path: str, query: str = '', body: bytes | None = None, ctype: str = 'application/json') -> tuple:
    """Return the project access a request needs."""
    return plan_request(POLICY, method, path, query, ctype, body).needs


class GitTest(unittest.TestCase):
    def test_fetch_needs_read(self) -> None:
        self.assertEqual(needs('GET', f'/{LIB}.git/info/refs', 'service=git-upload-pack'), ((LIB, 'read'),))
        self.assertEqual(needs('POST', f'/{LIB}/git-upload-pack', body=b'0000'), ((LIB, 'read'),))

    def test_push_to_feature_branch_needs_write(self) -> None:
        self.assertEqual(needs('GET', f'/{CODE}.git/info/refs', 'service=git-receive-pack'), ((CODE, 'write'),))
        body = push_body((OLD, NEW, 'refs/heads/feature/PILOT-1-x'))
        self.assertEqual(needs('POST', f'/{CODE}.git/git-receive-pack', body=body), ((CODE, 'write'),))

    def test_push_rules(self) -> None:
        for commands in (
            [(OLD, NEW, 'refs/heads/develop')],
            [(ZERO, NEW, 'refs/tags/v1.0.0')],
            [(OLD, ZERO, 'refs/heads/feature/PILOT-1-x')],
            [(OLD, NEW, 'refs/heads/feature/ok'), (OLD, NEW, 'refs/heads/main')],
        ):
            with self.subTest(commands=commands), self.assertRaises(Denied):
                needs('POST', f'/{CODE}.git/git-receive-pack', body=push_body(*commands))

    def test_refused_push_reports_per_ref(self) -> None:
        with self.assertRaises(Denied) as caught:
            needs('POST', f'/{CODE}.git/git-receive-pack', body=push_body((OLD, NEW, 'refs/heads/develop')))
        assert caught.exception.reply is not None
        content_type, reply = caught.exception.reply
        self.assertEqual(content_type, 'application/x-git-receive-pack-result')
        self.assertIn(b'ng refs/heads/develop cred-gw: pushing refs/heads/develop is not allowed', reply)

    def test_malformed_push_is_refused(self) -> None:
        for body in (b'', b'zzzz', b'0000PACK', pkt('only-two words') + b'0000', b'00ff' + b'x' * 10):
            with self.subTest(body=body), self.assertRaises(Denied):
                pushed_refs(body)

    def test_dumb_http_and_traversal_are_refused(self) -> None:
        for method, path, query in (
            ('GET', f'/{CODE}.git/info/refs', ''),
            ('GET', f'/{CODE}.git/HEAD', ''),
            ('GET', f'/{CODE}/../../shared/tools.git/info/refs', 'service=git-upload-pack'),
            ('GET', f'/{CODE}.git/%2e%2e/info/refs', 'service=git-upload-pack'),
        ):
            with self.subTest(path=path), self.assertRaises(Denied):
                needs(method, path, query)


class ApiTest(unittest.TestCase):
    def test_project_reads(self) -> None:
        encoded = CODE.replace('/', '%2F')
        self.assertEqual(needs('GET', f'{API}/projects/{encoded}/merge_requests'), ((CODE, 'read'),))
        self.assertEqual(needs('GET', f'{API}/projects/42'), (('42', 'read'),))
        self.assertEqual(needs('GET', f'{API}/user'), ())

    def test_user_lookup_by_exact_username(self) -> None:
        for query in ('username=jane.doe', 'username=u123456&per_page=100&page=1'):
            with self.subTest(query=query):
                self.assertEqual(plan_request(POLICY, 'GET', f'{API}/users', query, '', None).query, query)
        for method, path, query in (
            ('GET', f'{API}/users', ''),
            ('GET', f'{API}/users', 'search=a'),
            ('GET', f'{API}/users', 'username=a&search=b'),
            ('GET', f'{API}/users', 'username=a&username=b'),
            ('GET', f'{API}/users', 'username='),
            ('GET', f'{API}/users', 'username=a%20b'),
            ('GET', f'{API}/users', 'username=a&active=true'),
            ('POST', f'{API}/users', 'username=a'),
            ('GET', f'{API}/users/1', ''),
            ('GET', f'{API}/users/1/projects', ''),
        ):
            with self.subTest(method=method, path=path, query=query), self.assertRaises(Denied):
                needs(method, path, query)

    def test_secret_settings_and_other_endpoints_are_refused(self) -> None:
        for method, path in (
            ('GET', f'{API}/projects/42/variables'),
            ('GET', f'{API}/projects/42/access_tokens'),
            ('GET', f'{API}/projects/42/hooks/1'),
            ('GET', f'{API}/projects'),
            ('GET', f'{API}/groups/1/projects'),
            ('POST', '/api/graphql'),
            ('GET', '/grp/sub/example-code/-/blob/develop/README.md'),
            ('GET', f'{API}/projects/42/repository/files/a%2F..%2Fb'),
        ):
            with self.subTest(path=path), self.assertRaises(Denied):
                needs(method, path)

    def test_merge_request_writes(self) -> None:
        self.assertEqual(needs('POST', f'{API}/projects/42/merge_requests', body=b'{"title": "t"}'), (('42', 'write'),))
        self.assertEqual(needs('PUT', f'{API}/projects/42/merge_requests/7', body=b'{}'), (('42', 'write'),))
        self.assertEqual(needs('POST', f'{API}/projects/42/merge_requests/7/notes', body=b'{}'), (('42', 'write'),))
        body = b'title=t&target_project_id=99'
        self.assertEqual(
            needs('POST', f'{API}/projects/42/merge_requests', body=body, ctype='application/x-www-form-urlencoded'),
            (('42', 'write'), ('99', 'write')),
        )

    def test_other_writes_are_refused(self) -> None:
        for method, path in (
            ('PUT', f'{API}/projects/42/merge_requests/7/merge'),
            ('POST', f'{API}/projects/42/merge_requests/7/approve'),
            ('DELETE', f'{API}/projects/42/merge_requests/7'),
            ('POST', f'{API}/projects/42/repository/commits'),
            ('PUT', f'{API}/projects/42'),
            ('POST', f'{API}/projects/42/variables'),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(Denied):
                needs(method, path, body=b'{}')


class FakeGitLab:
    """Resolves numeric project ids."""

    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths
        self.lookups = 0

    def get_json(self, path_query: str) -> tuple[int, object]:
        self.lookups += 1
        project_id = path_query.rsplit('/', 1)[1]
        if project_id not in self.paths:
            return 404, {'message': '404 Project Not Found'}
        return 200, {'path_with_namespace': self.paths[project_id]}


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = GitLabService('git.test', POLICY)
        self.upstream = FakeGitLab({'42': 'Grp/Sub/example-code', '43': LIB, '44': 'grp/other/secret-project'})

    def authorize(self, method: str, path: str, query: str = '', body: bytes | None = None) -> str:
        return self.service.authorize(method, path, query, {'Content-Type': 'application/json'}, body, self.upstream)  # type: ignore[arg-type]

    def test_numeric_ids_are_resolved_once(self) -> None:
        self.authorize('GET', f'{API}/projects/42/merge_requests')
        self.authorize('POST', f'{API}/projects/42/merge_requests', body=b'{"title": "t"}')
        self.assertEqual(self.upstream.lookups, 1)

    def test_read_only_project_cannot_be_written(self) -> None:
        self.authorize('GET', f'{API}/projects/43')
        with self.assertRaises(Denied):
            self.authorize('POST', f'{API}/projects/43/merge_requests', body=b'{}')
        with self.assertRaises(Denied):
            self.authorize('GET', f'/{LIB}.git/info/refs', 'service=git-receive-pack')

    def test_unlisted_and_unknown_projects_are_refused(self) -> None:
        for path in (f'{API}/projects/44', f'{API}/projects/999', f'{API}/projects/grp%2Fother%2Fsecret-project'):
            with self.subTest(path=path), self.assertRaises(Denied):
                self.authorize('GET', path)

    def test_target_project_outside_write_list_is_refused(self) -> None:
        with self.assertRaises(Denied):
            self.authorize('POST', f'{API}/projects/42/merge_requests', body=b'{"target_project_id": 43}')


class AuthTest(unittest.TestCase):
    def test_api_uses_private_token_and_git_basic_auth(self) -> None:
        headers = gitlab_auth('t0ken')
        self.assertEqual(headers(f'{API}/user'), {'PRIVATE-TOKEN': 't0ken'})
        self.assertEqual(headers(f'/{CODE}.git/info/refs?service=git-upload-pack'), {'Authorization': 'Basic b2F1dGgyOnQwa2Vu'})


if __name__ == '__main__':
    unittest.main()
