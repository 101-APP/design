#!/usr/bin/env python3
"""Announce a verified design library release in personal 101 AI chats.

One pass: read the latest published npm version, confirm that the Git tag of
the same number points at the commit the package was built from, then deliver
one message per recipient. Texts and idempotency keys are stored before any
network side effect, so a repeated pass never sends a second message.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request

API_BASE = 'https://app.101-group.ru'
LOGIN_URL = API_BASE + '/api/user/login/'
MESSAGES_URL = API_BASE + '/api/assistant-messages/'
PACKAGE = '@101app/design-tokens-web'
REGISTRY_URL = 'https://registry.npmjs.org/@101app%2Fdesign-tokens-web/latest'
GITHUB_API = 'https://api.github.com/repos/101-APP/design/git/'
CONFIG_FILE = '/etc/design-release-notify/config.json'
STATE_DIRECTORY = '/var/lib/design-release-notify'
CREDENTIAL_NAME = 'notification-source.json'
MAX_BYTES = 65536
MAX_STORE_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 10
KEPT_RELEASES = 20
PERMANENT_STATUSES = {400, 403, 404, 409}

GUID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', re.I)
VERSION = re.compile(r'(?:0|[1-9]\d{0,5})(?:\.(?:0|[1-9]\d{0,5})){2}')
COMMIT = re.compile(r'[0-9a-f]{40}')
TOKEN = re.compile(r'[A-Za-z0-9._-]{20,8192}')
SAFE_DELIVERY_ERROR = re.compile(r'HTTP_\d{3}|RECEIPT_INVALID|AUTH_UNAVAILABLE')


class NotifyError(Exception):
    """Carries a safe code only; secrets and response bodies never reach it."""


def is_guid(value):
    return isinstance(value, str) and GUID.fullmatch(value) is not None


def same_guid(left, right):
    return is_guid(left) and is_guid(right) and left.lower() == right.lower()


def is_version(value):
    return isinstance(value, str) and VERSION.fullmatch(value) is not None


def version_key(value):
    return tuple(int(part) for part in value.split('.'))


def read_private_json(filename, max_bytes=MAX_BYTES):
    """A missing file raises FileNotFoundError; anything unsafe is rejected."""
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise
    except OSError:
        raise NotifyError('PRIVATE_FILE_INVALID') from None
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise NotifyError('PRIVATE_FILE_INVALID')
        try:
            return json.loads(handle.read(max_bytes + 1))
        except ValueError:
            raise NotifyError('PRIVATE_FILE_INVALID') from None


def write_private_json(filename, value):
    temporary = f'{filename}.{os.getpid()}.{os.urandom(8).hex()}.next'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, filename)
        directory = os.open(os.path.dirname(filename) or '.', os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_json(method, url, body=None, token=None):
    """Return (status, parsed JSON or None). Only the four known routes are reachable."""
    if url not in (LOGIN_URL, MESSAGES_URL, REGISTRY_URL) and not url.startswith(GITHUB_API):
        raise NotifyError('ROUTE_INVALID')
    # The sender key belongs to the 101 message route and never leaves it.
    if token is not None and url != MESSAGES_URL:
        raise NotifyError('ROUTE_INVALID')
    headers = {'Accept': 'application/json', 'User-Agent': 'design-release-notify'}
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token is not None:
        headers['Authorization'] = 'JWT ' + token
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                status, raw = response.status, response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        raise NotifyError('NETWORK_UNAVAILABLE') from None
    if len(raw) > MAX_BYTES:
        raise NotifyError('NETWORK_UNAVAILABLE')
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def validate_config(config):
    keys = {'format_version', 'sender_guid', 'recipients', 'announce_after_version'}
    if not isinstance(config, dict) or set(config) != keys:
        raise NotifyError('CONFIG_INVALID')
    recipients = config['recipients']
    if (type(config['format_version']) is not int or config['format_version'] != 1
            or not is_guid(config['sender_guid']) or not is_version(config['announce_after_version'])
            or not isinstance(recipients, list) or not 1 <= len(recipients) <= 10
            or not all(is_guid(guid) for guid in recipients)
            or len({guid.lower() for guid in recipients}) != len(recipients)):
        raise NotifyError('CONFIG_INVALID')


def validate_source(source, config):
    if (not isinstance(source, dict) or source.get('format_version') != 1 or source.get('base_url') != API_BASE
            or not same_guid(source.get('sender_guid'), config['sender_guid'])
            or not isinstance(source.get('email'), str) or not source['email'].strip()
            or not isinstance(source.get('password'), str) or not source['password']):
        raise NotifyError('SOURCE_INVALID')


def login(source, http):
    try:
        status, data = http('POST', LOGIN_URL, {'email': source['email'], 'password': source['password']})
    except NotifyError:
        raise NotifyError('AUTH_UNAVAILABLE') from None
    token = data.get('token') if isinstance(data, dict) else None
    # The pinned sender defines the server-side idempotency scope; another account must not send.
    if (status != 200 or not isinstance(token, str) or not TOKEN.fullmatch(token)
            or not same_guid(data.get('user_guid'), source['sender_guid'])):
        raise NotifyError('AUTH_UNAVAILABLE')
    return token


def fetch_latest(http):
    status, data = http('GET', REGISTRY_URL)
    if status != 200 or not isinstance(data, dict):
        raise NotifyError('REGISTRY_UNAVAILABLE')
    version, commit = data.get('version'), data.get('designTokensSourceCommit')
    if data.get('name') != PACKAGE or not is_version(version) or not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise NotifyError('REGISTRY_INVALID')
    return version, commit


def tag_commit(http, version):
    def target(status, data, expected_ref=None):
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get('object'), dict):
            raise NotifyError('GITHUB_UNAVAILABLE')
        if expected_ref is not None and data.get('ref') != expected_ref:
            raise NotifyError('GITHUB_UNAVAILABLE')
        kind, sha = data['object'].get('type'), data['object'].get('sha')
        if kind not in ('commit', 'tag') or not isinstance(sha, str) or not COMMIT.fullmatch(sha):
            raise NotifyError('GITHUB_UNAVAILABLE')
        return kind, sha

    status, data = http('GET', GITHUB_API + 'ref/tags/' + version)
    if status == 404:
        raise NotifyError('TAG_ABSENT')
    kind, sha = target(status, data, 'refs/tags/' + version)
    if kind == 'tag':
        kind, sha = target(*http('GET', GITHUB_API + 'tags/' + sha))
    if kind != 'commit':
        raise NotifyError('GITHUB_UNAVAILABLE')
    return sha


def release_text(version):
    return '\n'.join([
        'Библиотека дизайна 101 обновлена',
        f'**Версия:** {version}',
        f'**Web:** npm install {PACKAGE}@{version}',
        f'**iOS и Android:** тег {version}',
    ])


def client_message_id(version, guid):
    return hashlib.sha256(f'design-release:v1:{version}:{guid.lower()}'.encode('utf-8')).hexdigest()


def read_store(filename):
    try:
        store = read_private_json(filename, MAX_STORE_BYTES)
    except FileNotFoundError:
        return {'format_version': 1, 'releases': {}}
    releases = store.get('releases') if isinstance(store, dict) else None
    if not isinstance(releases, dict) or store.get('format_version') != 1:
        raise NotifyError('STORE_INVALID')
    for version, release in releases.items():
        if (not is_version(version) or not isinstance(release, dict) or not isinstance(release.get('text'), str)
                or not isinstance(release.get('recipients'), dict)):
            raise NotifyError('STORE_INVALID')
        for guid, delivery in release['recipients'].items():
            if (not is_guid(guid) or not isinstance(delivery, dict)
                    or delivery.get('state') not in ('pending', 'sent', 'blocked', 'superseded')
                    or not isinstance(delivery.get('client_message_id'), str)
                    or type(delivery.get('attempts')) is not int
                    or not isinstance(delivery.get('next_attempt_at'), (int, float))):
                raise NotifyError('STORE_INVALID')
    return store


def check_release(config, store, http):
    """Queue the latest release once npm and the Git tag agree on its source commit."""
    version, commit = fetch_latest(http)
    newest = max([config['announce_after_version'], *store['releases']], key=version_key)
    if version_key(version) <= version_key(newest):
        return version, 'not_new'
    if tag_commit(http, version) != commit:
        raise NotifyError('COMMIT_MISMATCH')
    # Only the current version is worth reading: an undelivered older one is dropped, not sent late.
    for release in store['releases'].values():
        for delivery in release['recipients'].values():
            if delivery['state'] == 'pending':
                delivery['state'] = 'superseded'
    store['releases'][version] = {
        'commit': commit,
        'text': release_text(version),
        'recipients': {guid: {'client_message_id': client_message_id(version, guid), 'state': 'pending',
                              'attempts': 0, 'next_attempt_at': 0} for guid in config['recipients']},
    }
    for old in sorted(store['releases'], key=version_key)[:-KEPT_RELEASES]:
        del store['releases'][old]
    return version, 'queued'


def deliver(config, source, store, save, http, now):
    allowed = {guid.lower() for guid in config['recipients']}
    token, auth_failed, sent = None, False, 0
    for version in sorted(store['releases'], key=version_key):
        release = store['releases'][version]
        for guid, delivery in release['recipients'].items():
            if delivery['state'] != 'pending' or guid.lower() not in allowed or delivery['next_attempt_at'] > now():
                continue
            try:
                if token is None:
                    if auth_failed:
                        raise NotifyError('AUTH_UNAVAILABLE')
                    try:
                        token = login(source, http)
                    except NotifyError:
                        auth_failed = True
                        raise
                status, data = http('POST', MESSAGES_URL, {'userGuid': guid, 'text': release['text'],
                                                           'clientMessageId': delivery['client_message_id']}, token)
                if status != 201:
                    raise NotifyError(f'HTTP_{status}')
                if (not isinstance(data, dict) or not same_guid(data.get('userGuid'), guid) or data.get('text') != release['text']
                        or not is_guid(data.get('messageGuid')) or not is_guid(data.get('chatGuid'))):
                    raise NotifyError('RECEIPT_INVALID')
                delivery.update(state='sent', message_guid=data['messageGuid'], chat_guid=data['chatGuid'],
                                accepted_at=datetime.datetime.fromtimestamp(now(), datetime.timezone.utc).isoformat())
                delivery.pop('last_error', None)
                sent += 1
            except NotifyError as error:
                code = str(error)
                delivery['attempts'] += 1
                delivery['last_error'] = code if SAFE_DELIVERY_ERROR.fullmatch(code) else 'API_UNAVAILABLE'
                status = int(code[5:]) if code.startswith('HTTP_') else 0
                if status in PERMANENT_STATUSES or 300 <= status < 400:
                    delivery['state'] = 'blocked'
                else:
                    delivery['next_attempt_at'] = now() + min(1800, 30 * 2 ** min(delivery['attempts'] - 1, 6))
            save()
    return sent


def run(config, source, store_file, http=http_json, now=time.time, dry_run=False):
    store = read_store(store_file)
    result = {}
    try:
        result['latest'], result['release'] = check_release(config, store, http)
    except NotifyError as error:
        # A failed check never blocks deliveries that were already queued.
        result['release'], result['reason'] = 'unverified', str(error)
    if dry_run:
        if result['release'] == 'queued':
            result.update(release='would_announce', text=store['releases'][result['latest']]['text'],
                          recipients=len(config['recipients']))
        return result

    def save():
        write_private_json(store_file, store)

    if result['release'] == 'queued':
        save()
    result['sent'] = deliver(config, source, store, save, http, now)
    states = [delivery['state'] for release in store['releases'].values() for delivery in release['recipients'].values()]
    result['pending'], result['blocked'] = states.count('pending'), states.count('blocked')
    return result


def acquire_lock(state_directory):
    try:
        info = os.lstat(state_directory)
    except OSError:
        raise NotifyError('STATE_DIRECTORY_INVALID') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise NotifyError('STATE_DIRECTORY_INVALID')
    descriptor = os.open(os.path.join(state_directory, 'run.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise NotifyError('ALREADY_RUNNING') from None
    return descriptor


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--check-auth', action='store_true', help='verify the sender login only; no messages are sent')
    parser.add_argument('--dry-run', action='store_true', help='check npm and the Git tag and print the message; no login, no state changes')
    parser.add_argument('--config', default=CONFIG_FILE)
    parser.add_argument('--state-dir', default=STATE_DIRECTORY)
    options = parser.parse_args(argv)
    label = '[design-release-notify]'
    try:
        try:
            config = read_private_json(options.config)
        except FileNotFoundError:
            raise NotifyError('CONFIG_ABSENT') from None
        validate_config(config)
        store_file = os.path.join(options.state_dir, 'state.json')
        if options.dry_run:
            print(label, json.dumps(run(config, None, store_file, dry_run=True), ensure_ascii=False))
            return 0
        credentials = os.environ.get('CREDENTIALS_DIRECTORY')
        if not credentials or not os.path.isabs(credentials):
            raise NotifyError('CREDENTIAL_ABSENT')
        try:
            source = read_private_json(os.path.join(credentials, CREDENTIAL_NAME))
        except FileNotFoundError:
            raise NotifyError('CREDENTIAL_ABSENT') from None
        validate_source(source, config)
        if options.check_auth:
            login(source, http_json)
            print(label, 'sender identity verified; no messages sent')
            return 0
        lock = acquire_lock(options.state_dir)
        try:
            print(label, json.dumps(run(config, source, store_file), ensure_ascii=False))
        finally:
            os.close(lock)
        return 0
    except NotifyError as error:
        print(label, f'failed: {error}', file=sys.stderr)
    except Exception as error:  # No traceback: it must never carry request data into the journal.
        print(label, f'failed: {type(error).__name__}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
