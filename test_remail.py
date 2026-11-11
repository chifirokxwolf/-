from datetime import datetime, timezone
from io import BytesIO
import json
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from remail import (Client, Credentials, Mailbox, MailError, Provider, _NoRedirects,
                    credentials, extract_login)

ACCOUNT = {'id': 1, 'email': 'test@example.org',
           'pickup': 'https://remail.aishop6.com/pickup?email=test%40example.org&token=local-fixture%2B%26'}
SENDER = 'login@claude.example'  # Fixture only; not a claimed real Claude sender.


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def msg(i, t=1001, **changes):
    return dict({'id': i, 'sender': SENDER, 'recipient': ACCOUNT['email'],
                 'receivedAt': iso(t), 'subject': 'Sign in', 'verificationCode': '012345'}, **changes)


class Clock:
    def __init__(self):
        self.now = 1000

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeClient:
    auth = Credentials(ACCOUNT['email'], 'fixture')

    def __init__(self, pages, detail=None):
        self.pages = iter(pages)
        self.detail = detail
        self.pickup_calls = 0
        self.detail_calls = []

    def pickup(self, timeout=15):
        self.pickup_calls += 1
        value = next(self.pages, ([], {}))
        if isinstance(value, Exception):
            raise value
        return value

    def message(self, i, timeout=15):
        self.detail_calls.append(i)
        return self.detail


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return BytesIO(json.dumps(self.response).encode())


class ApiTests(unittest.TestCase):
    def test_both_documented_endpoints_encode_original_credentials(self):
        opener = Opener({'items': [], 'fetch': {'lastStatus': 'pending'}})
        client = Client(ACCOUNT, opener=opener)
        self.assertEqual(client.pickup()[0], [])
        opener.response = msg(5012)
        self.assertEqual(client.message(5012)['id'], 5012)
        for request, timeout in opener.calls:
            self.assertEqual(parse_qs(urlsplit(request.full_url).query),
                             {'email': ['test@example.org'], 'token': ['local-fixture+&']})
            self.assertEqual(request.get_header('Accept'), 'application/json')
            self.assertGreater(timeout, 0)
        self.assertEqual(urlsplit(opener.calls[0][0].full_url).path, '/v1/pickup')
        self.assertEqual(urlsplit(opener.calls[1][0].full_url).path, '/v1/pickup/messages/5012')

    def test_errors_never_expose_url_or_body(self):
        for error in [HTTPError('https://example/?token=SECRET', 401, 'SECRET', {}, BytesIO(b'SECRET')),
                      URLError('SECRET')]:
            with self.assertRaises(MailError) as caught:
                Client(ACCOUNT, opener=Opener(error)).pickup()
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertNotIn('local-fixture', str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_http_retry_after_is_preserved(self):
        error = HTTPError('https://example', 429, 'rate limit', {'Retry-After': '25'}, BytesIO())
        with self.assertRaises(MailError) as caught:
            Client(ACCOUNT, opener=Opener(error)).pickup()
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 25)

    def test_reject_invalid_credentials_and_redirects(self):
        for url in [ACCOUNT['pickup'] + '&token=other',
                    ACCOUNT['pickup'].replace('test%40example.org', 'wrong%40example.org'),
                    ACCOUNT['pickup'].replace('https://', 'http://'),
                    ACCOUNT['pickup'].replace('remail.aishop6.com', 'attacker.example')]:
            with self.assertRaises(MailError):
                credentials(ACCOUNT['email'], url)
        self.assertIsNone(_NoRedirects().redirect_request(None, None, 302, '', {}, 'https://other/'))

    def test_unexpected_schema_fails(self):
        with self.assertRaises(MailError):
            Client(ACCOUNT, opener=Opener({'items': [{'id': '5012'}], 'fetch': {}})).pickup()


class MailboxTests(unittest.TestCase):
    def mailbox(self, client):
        self.clock = Clock()
        return Mailbox(client, [SENDER], poll_seconds=2, clock=self.clock.time,
                       monotonic=self.clock.time, sleep=self.clock.sleep)

    def test_skips_old_wrong_sender_and_wrong_recipient_then_reads_fresh_detail(self):
        fresh = msg(4)
        client = FakeClient([
            ([msg(1, 990)], {}),
            ([msg(1, 990), msg(2, 999), msg(3, 1000, sender='other@example.org')], {}),
            ([msg(5, recipient='other@example.org'), fresh], {})], fresh)
        mailbox = self.mailbox(client)
        result = mailbox.wait(mailbox.snapshot(), 10)
        self.assertEqual(result, {'value': '012345', 'message_id': 4})
        self.assertEqual(client.detail_calls, [4])

    def test_server_fetch_schedule_is_honored(self):
        client = FakeClient([([], {'nextFetchAllowedAt': iso(1007)}), ([msg(2, 1006)], {})], msg(2, 1006))
        mailbox = self.mailbox(client)
        mailbox.wait(mailbox.snapshot(), 10)
        self.assertEqual(self.clock.now, 1007)

    def test_retry_after_longer_than_timeout_does_not_poll_again(self):
        client = FakeClient([([], {}), MailError('Rate limit', retryable=True, retry_after=60)])
        mailbox = self.mailbox(client)
        with self.assertRaisesRegex(MailError, 'Timed out'):
            mailbox.wait(mailbox.snapshot(), 10)
        self.assertEqual(client.pickup_calls, 2)
        self.assertEqual(self.clock.now, 1010)

    def test_invalid_auth_is_not_retried(self):
        client = FakeClient([([], {}), MailError('Remail HTTP 401')])
        mailbox = self.mailbox(client)
        with self.assertRaisesRegex(MailError, '401'):
            mailbox.wait(mailbox.snapshot(), 10)
        self.assertEqual(client.pickup_calls, 2)

    def test_ambiguous_messages_are_not_delivered(self):
        client = FakeClient([([], {}), ([msg(1, 1000), msg(2, 1000)], {})])
        mailbox = self.mailbox(client)
        with self.assertRaisesRegex(MailError, 'Multiple fresh'):
            mailbox.wait(mailbox.snapshot(), 10)
        self.assertEqual(client.detail_calls, [])

    def test_detail_for_another_mailbox_is_rejected(self):
        client = FakeClient([([], {}), ([msg(1, 1000)], {})], msg(1, 1000, recipient='other@example.org'))
        mailbox = self.mailbox(client)
        with self.assertRaisesRegex(MailError, 'do not match'):
            mailbox.wait(mailbox.snapshot(), 10)

    def test_empty_sender_allowlist_fails(self):
        with self.assertRaises(MailError):
            Mailbox(FakeClient([]), [])

    def test_extracts_html_link_and_preserves_leading_zero_code(self):
        self.assertEqual(extract_login(msg(1)), '012345')
        body = '<a href="https://claude.ai/magic?token=fixture&amp;state=x">Sign in</a>'
        self.assertEqual(extract_login({'body': body}), 'https://claude.ai/magic?token=fixture&state=x')
        for body in ['https://claude.ai.attacker.example/login?token=x',
                     '<a href="https://remail.aishop6.com/pickup?token=x">Mail</a>',
                     'https://claude.ai/magic?t=a https://claude.ai/magic?t=b']:
            with self.assertRaises(MailError):
                extract_login({'body': body})

    def test_provider_snapshots_before_requesting_login(self):
        sequence = []
        class Claude:
            def request_login(self, account, order_id):
                sequence.append(('login', order_id))
        client = FakeClient([])
        provider = Provider(Claude(), {'allowed_senders': [SENDER]}, lambda a: client)
        with patch('remail.Mailbox.snapshot', side_effect=lambda: sequence.append('snapshot') or 'snapshot-value'), \
             patch('remail.Mailbox.wait', side_effect=lambda s, t: sequence.append(('wait', s)) or {'value': '012345', 'message_id': 1}):
            self.assertEqual(provider.issue(ACCOUNT, 'order')['message_id'], 1)
        self.assertEqual(sequence, ['snapshot', ('login', 'order'), ('wait', 'snapshot-value')])


if __name__ == '__main__':
    unittest.main()
