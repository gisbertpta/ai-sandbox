"""Tests for the Jira rules. Run in cred-gw/: python3 -m unittest -v (stdlib only)."""

from __future__ import annotations

import unittest
import urllib.parse

from core import Denied
from jira_policy import AGILE, API, JiraService, Plan, Policy, attachment_ids, parse_body, plan_request, split_order_by

LABEL = 'Widerspruchsmanager'
PROJECTS = frozenset({'PILOT'})
READ_ONLY = Policy(label=LABEL, projects=PROJECTS, allow_writes=False)
WRITES = Policy(label=LABEL, projects=PROJECTS, allow_writes=True)


def jql_of(plan: Plan) -> str:
    """Return the jql parameter of a planned search query."""
    return dict(urllib.parse.parse_qsl(plan.query))['jql']


class ReadRulesTest(unittest.TestCase):
    def test_issue_reads_need_label_check(self) -> None:
        for path in (
            f'{API}/issue/PILOT-1',
            f'{API}/issue/PILOT-1/comment',
            f'{API}/issue/12345/transitions',
            f'{API}/issue/PILOT-1/remotelink',
            f'{API}/issue/PILOT-1/worklog',
        ):
            plan = plan_request(READ_ONLY, 'GET', path, 'startAt=0', None)
            self.assertEqual(plan.check_keys, (path.split('/')[5],))
            self.assertEqual(plan.query, 'startAt=0')

    def test_metadata_passes_without_checks(self) -> None:
        for path in (
            f'{API}/myself',
            f'{API}/field',
            f'{API}/status/3',
            f'{API}/issuetype',
            f'{API}/user',
            f'{AGILE}/board/12/configuration',
            f'{AGILE}/sprint/5',
        ):
            with self.subTest(path=path):
                self.assertEqual(plan_request(READ_ONLY, 'GET', path, 'username=x', None), Plan((), 'username=x'))

    def test_project_endpoints_only_for_own_projects(self) -> None:
        for path, query in (
            (f'{API}/project/PILOT', ''),
            (f'{API}/project/pilot/components', ''),
            (f'{API}/issue/createmeta/PILOT/issuetypes/10006', ''),
            (f'{AGILE}/board', 'projectKeyOrId=PILOT'),
        ):
            with self.subTest(path=path):
                self.assertEqual(plan_request(READ_ONLY, 'GET', path, query, None).check_keys, ())
        for path, query in (
            (f'{API}/project/OTHER/versions', ''),
            (f'{API}/issue/createmeta/OTHER/issuetypes', ''),
            (f'{AGILE}/board', ''),
            (f'{AGILE}/board', 'projectKeyOrId=OTHER'),
        ):
            with self.subTest(path=path, query=query), self.assertRaises(Denied):
                plan_request(READ_ONLY, 'GET', path, query, None)

    def test_other_endpoints_are_denied(self) -> None:
        for method, path in (
            ('GET', f'{API}/project'),
            ('GET', f'{API}/user/search'),
            ('GET', f'{API}/filter/1'),
            ('GET', f'{API}/dashboard'),
            ('GET', f'{API}/issue/picker'),
            ('GET', f'{API}/jql/autocompletedata'),
            ('GET', f'{API}/issue/PILOT-1/comment/123'),
            ('GET', f'{AGILE}/board/1/epic'),
            ('GET', '/secure/attachment/1/..'),
            ('POST', f'{API}/search'),
            ('DELETE', f'{API}/issue/PILOT-1'),
            ('POST', f'{API}/issueLink'),
            ('GET', f'{API}/issue/PILOT-1/../../project'),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(Denied):
                plan_request(WRITES, method, path, '', None)

    def test_writes_are_denied_when_read_only(self) -> None:
        with self.assertRaises(Denied) as caught:
            plan_request(READ_ONLY, 'POST', f'{API}/issue/PILOT-1/comment', '', {'body': 'x'})
        self.assertEqual(caught.exception.status, 403)


class SearchRulesTest(unittest.TestCase):
    def test_jql_is_wrapped_with_label(self) -> None:
        plan = plan_request(READ_ONLY, 'GET', f'{API}/search', 'jql=status%3DOpen&maxResults=5', None)
        self.assertEqual(jql_of(plan), f'(status=Open) AND labels = "{LABEL}"')
        self.assertIn('maxResults=5', plan.query)
        self.assertEqual(plan.check_keys, ())

    def test_order_by_stays_last(self) -> None:
        plan = plan_request(READ_ONLY, 'GET', f'{API}/search', 'jql=' + urllib.parse.quote('a = 1 order by created'), None)
        self.assertEqual(jql_of(plan), f'(a = 1 ) AND labels = "{LABEL}" order by created')

    def test_order_by_inside_quotes_is_text(self) -> None:
        condition, order_by = split_order_by('summary ~ "order by" and x = 1')
        self.assertEqual((condition, order_by), ('summary ~ "order by" and x = 1', ''))

    def test_breakout_attempts_are_denied(self) -> None:
        for jql in ('x = 1) OR (project = OTHER', 'x = 1 OR (y = 2', 'summary ~ "open', 'a = b\\) OR c = d', ''):
            with self.subTest(jql=jql), self.assertRaises(Denied):
                plan_request(READ_ONLY, 'GET', f'{API}/search', urllib.parse.urlencode({'jql': jql}), None)

    def test_quoted_parentheses_are_allowed(self) -> None:
        plan = plan_request(READ_ONLY, 'GET', f'{API}/search', urllib.parse.urlencode({'jql': 'summary ~ ")("'}), None)
        self.assertEqual(jql_of(plan), f'(summary ~ ")(") AND labels = "{LABEL}"')

    def test_duplicate_jql_is_denied(self) -> None:
        with self.assertRaises(Denied):
            plan_request(READ_ONLY, 'GET', f'{API}/search', 'jql=a%3D1&jql=b%3D2', None)

    def test_agile_issue_lists_are_wrapped(self) -> None:
        for path in (
            f'{AGILE}/board/1/issue',
            f'{AGILE}/board/1/backlog',
            f'{AGILE}/board/1/sprint/7/issue',
            f'{AGILE}/board/1/epic/none/issue',
            f'{AGILE}/sprint/7/issue',
            f'{AGILE}/epic/PILOT-3/issue',
        ):
            with self.subTest(path=path):
                self.assertEqual(jql_of(plan_request(READ_ONLY, 'GET', path, '', None)), f'labels = "{LABEL}"')
        plan = plan_request(READ_ONLY, 'GET', f'{AGILE}/board/1/issue', 'jql=status%3DOpen', None)
        self.assertEqual(jql_of(plan), f'(status=Open) AND labels = "{LABEL}"')
        with self.assertRaises(Denied):
            plan_request(READ_ONLY, 'GET', f'{AGILE}/board/1/issue', 'jql=x%3D1%29+OR+%28y%3D2', None)


class WriteRulesTest(unittest.TestCase):
    def test_edit_keeping_label_passes(self) -> None:
        body = {'fields': {'summary': 's', 'labels': ['sdd', LABEL]}}
        self.assertEqual(plan_request(WRITES, 'PUT', f'{API}/issue/PILOT-1', '', body).check_keys, ('PILOT-1',))

    def test_edit_removing_label_is_denied(self) -> None:
        for body in ({'fields': {'labels': ['sdd']}}, {'update': {'labels': [{'remove': LABEL}]}}, None, []):
            with self.subTest(body=body), self.assertRaises(Denied):
                plan_request(WRITES, 'PUT', f'{API}/issue/PILOT-1', '', body)

    def test_transition_with_label_removal_is_denied(self) -> None:
        with self.assertRaises(Denied):
            plan_request(WRITES, 'POST', f'{API}/issue/PILOT-1/transitions', '', {'fields': {'labels': []}})

    def test_create_requires_label(self) -> None:
        with self.assertRaises(Denied):
            plan_request(WRITES, 'POST', f'{API}/issue', '', {'fields': {'summary': 's', 'labels': ['sdd']}})
        plan = plan_request(WRITES, 'POST', f'{API}/issue', '', {'fields': {'summary': 's', 'labels': [LABEL]}})
        self.assertEqual(plan.check_keys, ())

    def test_subtask_checks_parent(self) -> None:
        body = {'fields': {'labels': [LABEL.lower()], 'parent': {'key': 'PILOT-7'}}}
        self.assertEqual(plan_request(WRITES, 'POST', f'{API}/issue', '', body).check_keys, ('PILOT-7',))
        with self.assertRaises(Denied):
            plan_request(WRITES, 'POST', f'{API}/issue', '', {'fields': {'labels': [LABEL], 'parent': {}}})


class AttachmentTest(unittest.TestCase):
    def test_attachment_paths_name_the_id(self) -> None:
        for path in ('/secure/attachment/42/scan.pdf', '/secure/thumbnail/42/_thumb_42.png', f'{API}/attachment/42'):
            with self.subTest(path=path):
                self.assertEqual(plan_request(READ_ONLY, 'GET', path, '', None).attachment, '42')

    def test_ids_are_collected_from_issue_and_search(self) -> None:
        issue = {'fields': {'attachment': [{'id': '1'}, {'id': 2}, {'id': 'x'}]}}
        self.assertEqual(attachment_ids(issue), {'1', '2'})
        self.assertEqual(attachment_ids({'issues': [issue, {'fields': {}}]}), {'1', '2'})
        self.assertEqual(attachment_ids([]), set())


class FakeJira:
    """Answers label lookups; issues in `labelled` carry the label."""

    def __init__(self, labelled: set[str]) -> None:
        self.labelled = labelled

    def get_json(self, path_query: str) -> tuple[int, object]:
        key = path_query.split('/')[5].split('?')[0]
        return 200, {'fields': {'labels': [LABEL] if key in self.labelled else ['other']}}


class ServiceTest(unittest.TestCase):
    def test_labelled_issue_passes(self) -> None:
        service = JiraService('jira.test', WRITES)
        query = service.authorize('GET', f'{API}/issue/PILOT-1', 'fields=summary', {}, None, FakeJira({'PILOT-1'}))  # type: ignore[arg-type]
        self.assertEqual(query, 'fields=summary')

    def test_unlabelled_issue_is_refused(self) -> None:
        service = JiraService('jira.test', WRITES)
        with self.assertRaises(Denied) as caught:
            service.authorize('POST', f'{API}/issue/OTHER-9/comment', '', {}, b'{"body": "x"}', FakeJira(set()))  # type: ignore[arg-type]
        self.assertEqual(caught.exception.status, 403)

    def test_attachment_needs_a_labelled_read_first(self) -> None:
        service = JiraService('jira.test', READ_ONLY)
        fake = FakeJira({'PILOT-1'})
        with self.assertRaises(Denied):
            service.authorize('GET', '/secure/attachment/42/scan.pdf', '', {}, None, fake)  # type: ignore[arg-type]
        observer = service.response_observer('GET', f'{API}/issue/PILOT-1')
        assert observer is not None
        observer(b'{"fields": {"attachment": [{"id": "42"}]}}')
        self.assertEqual(service.authorize('GET', '/secure/attachment/42/scan.pdf', '', {}, None, fake), '')  # type: ignore[arg-type]

    def test_only_issue_reads_and_searches_are_observed(self) -> None:
        service = JiraService('jira.test', READ_ONLY)
        for method, path in (('GET', f'{API}/search'), ('GET', f'{AGILE}/sprint/7/issue')):
            self.assertIsNotNone(service.response_observer(method, path))
        for method, path in (('GET', f'{API}/issue/PILOT-1/comment'), ('PUT', f'{API}/issue/PILOT-1'), ('GET', f'{API}/field')):
            self.assertIsNone(service.response_observer(method, path))
        service.remember_attachments(b'\x1f\x8b not json')
        self.assertEqual(service.attachments, set())

    def test_invalid_json_is_refused(self) -> None:
        with self.assertRaises(Denied):
            parse_body(b'{')


if __name__ == '__main__':
    unittest.main()
