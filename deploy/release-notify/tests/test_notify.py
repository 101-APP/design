import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('notify', pathlib.Path(__file__).resolve().parents[1] / 'notify.py')
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)

SENDER = '00000000-0000-4000-8000-000000000001'
FIRST = '00000000-0000-4000-8000-000000000002'
SECOND = 'AAAAAAAA-0000-4000-8000-000000000003'
COMMIT = 'a' * 40
TOKEN = 'header.payload.signature-0123456789'
PASSWORD = 'never-printed-password'


def config(recipients=(FIRST, SECOND), after='1.0.202'):
    return {'format_version': 1, 'sender_guid': SENDER, 'recipients': list(recipients), 'announce_after_version': after}


def source():
    return {'format_version': 1, 'base_url': notify.API_BASE, 'email': 'sender@example.test', 'password': PASSWORD, 'sender_guid': SENDER}


class Api:
    """Stands in for npm, GitHub and 101; records every call."""

    def __init__(self, version='1.0.203', commit=COMMIT, tag=COMMIT):
        self.version, self.commit, self.tag = version, commit, tag
        self.calls, self.messages = [], {}
        self.login_status, self.send_status, self.annotated = 200, {}, False

    def __call__(self, method, url, body=None, token=None):
        self.calls.append((method, url, body, token))
        if url == notify.REGISTRY_URL:
            return 200, {'name': notify.PACKAGE, 'version': self.version, 'designTokensSourceCommit': self.commit}
        if url == notify.GITHUB_API + 'ref/tags/' + self.version:
            if self.tag is None:
                return 404, {'message': 'Not Found'}
            kind, sha = ('tag', 'b' * 40) if self.annotated else ('commit', self.tag)
            return 200, {'ref': 'refs/tags/' + self.version, 'object': {'type': kind, 'sha': sha}}
        if url == notify.GITHUB_API + 'tags/' + 'b' * 40:
            return 200, {'object': {'type': 'commit', 'sha': self.tag}}
        if url == notify.LOGIN_URL:
            return self.login_status, {'token': TOKEN, 'user_guid': SENDER.upper()}
        if url == notify.MESSAGES_URL:
            status = self.send_status.get(body['userGuid'], 201)
            if status != 201:
                return status, {'code': 9528}
            # The server returns the first message for a repeated key, as the real API does.
            stored = self.messages.setdefault((body['userGuid'], body['clientMessageId']), body['text'])
            return 201, {'messageGuid': '11111111-1111-4111-8111-111111111111', 'chatGuid': '22222222-2222-4222-8222-222222222222',
                         'userGuid': body['userGuid'].upper(), 'text': stored, 'createdAt': '2026-11-23T12:00:00+03:00'}
        raise AssertionError(f'unexpected route {url}')

    def count(self, url):
        return sum(1 for call in self.calls if call[1] == url)


class ReleaseNotifyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = pathlib.Path(directory.name)
        self.store = str(self.directory / 'state.json')
        self.now = 1_000_000.0

    def run_pass(self, api, **options):
        return notify.run(options.pop('config', config()), source(), self.store, http=api, now=lambda: self.now, **options)

    def saved(self):
        return json.loads(pathlib.Path(self.store).read_text())

    def test_verified_release_reaches_every_recipient_once(self):
        api = Api()
        result = self.run_pass(api)
        self.assertEqual(result, {'latest': '1.0.203', 'release': 'queued', 'sent': 2, 'pending': 0, 'blocked': 0})
        text = 'Библиотека дизайна 101 обновлена\n**Версия:** 1.0.203\n**Web:** npm install @101app/design-tokens-web@1.0.203\n**iOS и Android:** тег 1.0.203'
        posts = [call for call in api.calls if call[1] == notify.MESSAGES_URL]
        self.assertEqual([call[2]['userGuid'] for call in posts], [FIRST, SECOND])
        self.assertEqual({call[2]['text'] for call in posts}, {text})
        self.assertEqual(len({call[2]['clientMessageId'] for call in posts}), 2)
        self.assertTrue(all(len(call[2]['clientMessageId']) == 64 for call in posts))
        self.assertEqual(api.count(notify.LOGIN_URL), 1)
        self.assertEqual({delivery['state'] for delivery in self.saved()['releases']['1.0.203']['recipients'].values()}, {'sent'})
        self.assertEqual(os.stat(self.store).st_mode & 0o777, 0o600)

        repeat = Api()
        self.assertEqual(self.run_pass(repeat), {'latest': '1.0.203', 'release': 'not_new', 'sent': 0, 'pending': 0, 'blocked': 0})
        self.assertEqual([call[1] for call in repeat.calls], [notify.REGISTRY_URL])

    def test_text_and_keys_are_stored_before_the_first_login_or_send(self):
        api, seen = Api(), []

        def watching(method, url, body=None, token=None):
            if url in (notify.LOGIN_URL, notify.MESSAGES_URL) and not seen:
                seen.append(self.saved()['releases']['1.0.203'])
            return api(method, url, body, token)

        notify.run(config(), source(), self.store, http=watching, now=lambda: self.now)
        self.assertEqual({delivery['state'] for delivery in seen[0]['recipients'].values()}, {'pending'})
        self.assertEqual(seen[0]['recipients'][FIRST]['client_message_id'], notify.client_message_id('1.0.203', FIRST))
        self.assertIn('1.0.203', seen[0]['text'])

    def test_already_announced_boundary_needs_no_tag_check_or_login(self):
        api = Api(version='1.0.202')
        self.assertEqual(self.run_pass(api)['release'], 'not_new')
        self.assertEqual([call[1] for call in api.calls], [notify.REGISTRY_URL])
        self.assertFalse(os.path.exists(self.store))

    def test_release_is_not_announced_until_tag_and_package_agree(self):
        for api, reason in [(Api(tag='c' * 40), 'COMMIT_MISMATCH'), (Api(tag=None), 'TAG_ABSENT')]:
            with self.subTest(reason=reason):
                result = self.run_pass(api)
                self.assertEqual((result['release'], result['reason'], result['sent']), ('unverified', reason, 0))
                self.assertEqual(api.count(notify.LOGIN_URL) + api.count(notify.MESSAGES_URL), 0)
                self.assertFalse(os.path.exists(self.store))

    def test_annotated_tag_is_resolved_to_its_commit(self):
        api = Api()
        api.annotated = True
        self.assertEqual(self.run_pass(api)['sent'], 2)

    def test_invalid_registry_answer_is_rejected(self):
        for field, value in [('version', '1.0.203-beta'), ('version', '01.0.203'), ('designTokensSourceCommit', 'abc'), ('name', 'other')]:
            with self.subTest(field=field, value=value):
                api = Api()
                answer = {'name': notify.PACKAGE, 'version': '1.0.203', 'designTokensSourceCommit': COMMIT, field: value}
                with self.assertRaises(notify.NotifyError):
                    notify.fetch_latest(lambda method, url, body=None, token=None: (200, answer))

    def test_temporary_failure_retries_later_with_the_same_key(self):
        api = Api()
        api.send_status[SECOND] = 503
        result = self.run_pass(api)
        self.assertEqual((result['sent'], result['pending'], result['blocked']), (1, 1, 0))
        failed = self.saved()['releases']['1.0.203']['recipients'][SECOND]
        self.assertEqual((failed['last_error'], failed['attempts'], failed['next_attempt_at']), ('HTTP_503', 1, self.now + 30))
        first_key = [call[2]['clientMessageId'] for call in api.calls if call[1] == notify.MESSAGES_URL][-1]

        early = Api()
        self.assertEqual(self.run_pass(early)['sent'], 0)
        self.assertEqual(early.count(notify.LOGIN_URL), 0)

        self.now += 31
        later = Api()
        self.assertEqual(self.run_pass(later), {'latest': '1.0.203', 'release': 'not_new', 'sent': 1, 'pending': 0, 'blocked': 0})
        posts = [call for call in later.calls if call[1] == notify.MESSAGES_URL]
        self.assertEqual([(call[2]['userGuid'], call[2]['clientMessageId']) for call in posts], [(SECOND, first_key)])

    def test_permanent_refusal_is_blocked_and_never_resent(self):
        for status in (400, 403, 404, 409, 302):
            with self.subTest(status=status):
                if os.path.exists(self.store):
                    os.unlink(self.store)
                api = Api()
                api.send_status[FIRST] = status
                result = self.run_pass(api)
                self.assertEqual((result['sent'], result['blocked']), (1, 1))
                self.now += 100_000
                again = Api()
                self.assertEqual(self.run_pass(again)['blocked'], 1)
                self.assertEqual(again.count(notify.MESSAGES_URL), 0)

    def test_failed_login_keeps_messages_pending_without_leaking_the_password(self):
        api = Api()
        api.login_status = 401
        result = self.run_pass(api)
        self.assertEqual((result['sent'], result['pending']), (0, 2))
        self.assertEqual(api.count(notify.LOGIN_URL), 1)
        self.assertEqual(api.count(notify.MESSAGES_URL), 0)
        stored = pathlib.Path(self.store).read_text()
        self.assertIn('AUTH_UNAVAILABLE', stored)
        self.assertNotIn(PASSWORD, stored + json.dumps(result))
        self.assertNotIn(TOKEN, stored)

    def test_receipt_for_another_person_or_text_is_not_accepted(self):
        api = Api()
        api.messages[(FIRST, notify.client_message_id('1.0.203', FIRST))] = 'earlier text under the same key'
        result = self.run_pass(api)
        self.assertEqual((result['sent'], result['pending']), (1, 1))
        self.assertEqual(self.saved()['releases']['1.0.203']['recipients'][FIRST]['last_error'], 'RECEIPT_INVALID')

    def test_newer_release_replaces_an_undelivered_older_one(self):
        api = Api()
        api.send_status[SECOND] = 500
        self.run_pass(api)
        self.now += 10_000
        newer = Api(version='1.0.204')
        result = self.run_pass(newer)
        self.assertEqual((result['release'], result['sent'], result['pending']), ('queued', 2, 0))
        self.assertTrue(all('1.0.204' in call[2]['text'] for call in newer.calls if call[1] == notify.MESSAGES_URL))
        self.assertEqual(self.saved()['releases']['1.0.203']['recipients'][SECOND]['state'], 'superseded')

    def test_registry_outage_does_not_stop_queued_deliveries(self):
        api = Api()
        api.send_status[SECOND] = 500
        self.run_pass(api)
        self.now += 31

        def outage(method, url, body=None, token=None):
            if url == notify.REGISTRY_URL:
                raise notify.NotifyError('NETWORK_UNAVAILABLE')
            return healthy(method, url, body, token)

        healthy = Api()
        result = notify.run(config(), source(), self.store, http=outage, now=lambda: self.now)
        self.assertEqual((result['release'], result['reason'], result['sent']), ('unverified', 'NETWORK_UNAVAILABLE', 1))

    def test_recipient_removed_from_config_is_not_messaged(self):
        api = Api()
        api.send_status[SECOND] = 500
        self.run_pass(api)
        self.now += 31
        later = Api()
        self.assertEqual(self.run_pass(later, config=config(recipients=[FIRST]))['sent'], 0)
        self.assertEqual(later.count(notify.MESSAGES_URL), 0)

    def test_dry_run_prints_the_message_without_login_or_state(self):
        api = Api()
        result = notify.run(config(), None, self.store, http=api, dry_run=True)
        self.assertEqual((result['release'], result['recipients']), ('would_announce', 2))
        self.assertIn('1.0.203', result['text'])
        self.assertEqual(api.count(notify.LOGIN_URL) + api.count(notify.MESSAGES_URL), 0)
        self.assertFalse(os.path.exists(self.store))

    def test_sender_key_and_unknown_routes_never_leave_the_process(self):
        for url, token in [(notify.REGISTRY_URL, TOKEN), (notify.GITHUB_API + 'ref/tags/1.0.203', TOKEN),
                           (notify.LOGIN_URL, TOKEN), ('https://example.test/api/assistant-messages/', None),
                           (notify.API_BASE + '/api/other/', None)]:
            with self.subTest(url=url), self.assertRaises(notify.NotifyError) as raised:
                notify.http_json('GET', url, token=token)
            self.assertEqual(str(raised.exception), 'ROUTE_INVALID')

    def test_config_is_strict(self):
        notify.validate_config(config())
        broken = [config(recipients=[]), config(recipients=[FIRST, FIRST.upper()]), config(recipients=['not-a-guid']),
                  config(after='1.0'), {**config(), 'format_version': 2}, {**config(), 'format_version': True},
                  {**config(), 'extra': 1}, {key: value for key, value in config().items() if key != 'sender_guid'}, []]
        for value in broken:
            with self.subTest(value=value), self.assertRaises(notify.NotifyError):
                notify.validate_config(value)

    def test_source_must_match_the_pinned_sender_and_host(self):
        notify.validate_source(source(), config())
        for change in [{'sender_guid': FIRST}, {'base_url': 'https://example.test'}, {'password': ''}, {'email': ' '}, {'format_version': 2}]:
            with self.subTest(change=change), self.assertRaises(notify.NotifyError):
                notify.validate_source({**source(), **change}, config())

    def test_login_rejects_another_account(self):
        def other_account(method, url, body=None, token=None):
            return 200, {'token': TOKEN, 'user_guid': FIRST}
        with self.assertRaises(notify.NotifyError):
            notify.login(source(), other_account)
        with self.assertRaises(notify.NotifyError):
            notify.login(source(), lambda *args, **kwargs: (200, None))

    def test_private_files_must_be_closed_regular_files(self):
        target = self.directory / 'config.json'
        target.write_text(json.dumps(config()))
        os.chmod(target, 0o644)
        with self.assertRaises(notify.NotifyError):
            notify.read_private_json(str(target))
        os.chmod(target, 0o600)
        self.assertEqual(notify.read_private_json(str(target)), config())
        link = self.directory / 'link.json'
        link.symlink_to(target)
        with self.assertRaises(notify.NotifyError):
            notify.read_private_json(str(link))
        target.write_text('{broken')
        with self.assertRaises(notify.NotifyError):
            notify.read_private_json(str(target))

    def test_corrupt_store_stops_the_pass(self):
        pathlib.Path(self.store).write_text(json.dumps({'format_version': 1, 'releases': {'1.0.203': {'text': 'x', 'recipients': {FIRST: {'state': 'sent'}}}}}))
        os.chmod(self.store, 0o600)
        with self.assertRaises(notify.NotifyError) as raised:
            self.run_pass(Api())
        self.assertEqual(str(raised.exception), 'STORE_INVALID')

    def test_only_recent_releases_are_kept(self):
        for patch in range(203, 203 + notify.KEPT_RELEASES + 3):
            self.run_pass(Api(version=f'1.0.{patch}'))
        releases = self.saved()['releases']
        self.assertEqual(len(releases), notify.KEPT_RELEASES)
        self.assertNotIn('1.0.203', releases)

    def test_second_process_cannot_start_while_the_first_holds_the_lock(self):
        os.chmod(self.directory, 0o700)
        lock = notify.acquire_lock(str(self.directory))
        try:
            with self.assertRaises(notify.NotifyError) as raised:
                notify.acquire_lock(str(self.directory))
            self.assertEqual(str(raised.exception), 'ALREADY_RUNNING')
        finally:
            os.close(lock)
        os.chmod(self.directory, 0o755)
        with self.assertRaises(notify.NotifyError):
            notify.acquire_lock(str(self.directory))

    def test_command_reports_safe_codes_only(self):
        missing = str(self.directory / 'absent.json')
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(notify.main(['--config', missing, '--state-dir', str(self.directory)]), 1)
        self.assertEqual(errors.getvalue().strip(), '[design-release-notify] failed: CONFIG_ABSENT')

        target = self.directory / 'config.json'
        target.write_text(json.dumps(config()))
        os.chmod(target, 0o600)
        previous = os.environ.pop('CREDENTIALS_DIRECTORY', None)
        self.addCleanup(lambda: previous is not None and os.environ.__setitem__('CREDENTIALS_DIRECTORY', previous))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(notify.main(['--config', str(target), '--state-dir', str(self.directory)]), 1)
        self.assertEqual(errors.getvalue().strip(), '[design-release-notify] failed: CREDENTIAL_ABSENT')


if __name__ == '__main__':
    unittest.main()
