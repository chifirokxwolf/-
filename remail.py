# noplug
"""Remail pickup API, using the contract supplied from /docs.

No dependency on Cardinal. Tokens, messages and credentials are never logged.
"""
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE_URL = 'https://remail.aishop6.com'


class MailError(Exception):
    """Safe to display: never includes an HTTP URL or response body."""
    def __init__(self, message, *, retryable=False, retry_after=0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True, repr=False)
class Credentials:
    email: str
    token: str


def credentials(email, pickup):
    try:
        u = urlsplit(pickup)
        q = parse_qs(u.query, keep_blank_values=True)
        valid = (u.scheme == 'https' and u.hostname == 'remail.aishop6.com'
                 and u.port in (None, 443) and not u.username and not u.password
                 and u.path == '/pickup' and not u.fragment
                 and q.get('email') == [email] and len(q.get('token', [])) == 1
                 and bool(q['token'][0]) and '@' in email)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise MailError('Pickup URL must contain the exact account email and one nonempty token')
    return Credentials(email, q['token'][0])


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def login_value(value):
    if not isinstance(value, str):
        raise MailError('Invalid login credential')
    if re.fullmatch(r'[0-9]{4,12}', value):
        return value
    try:
        u = urlsplit(value)
        if (u.scheme == 'https' and u.hostname == 'claude.ai' and not u.username
                and not u.password and u.port in (None, 443) and u.path not in ('', '/')
                and u.query and not any(c.isspace() or ord(c) < 32 for c in value)):
            return value
    except ValueError:
        pass
    raise MailError('Expected a numeric code or an HTTPS Claude login link')


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.values.extend(v for k, v in attrs if k == 'href' and v)


def extract_login(message):
    code = message.get('verificationCode')
    if isinstance(code, str) and re.fullmatch(r'[0-9]{4,12}', code.strip()):
        return code.strip()
    body = message.get('body') or message.get('bodyPreview') or ''
    if not isinstance(body, str):
        raise MailError('Invalid message body')
    links = _Links()
    links.feed(body)
    candidates = links.values + re.findall(r'https://[^\s<>"\']+', unescape(body))
    found = set()
    for candidate in candidates:
        try:
            found.add(login_value(candidate))
        except MailError:
            continue
    if len(found) != 1:
        raise MailError('No unambiguous Claude login link or verification code in message')
    return found.pop()


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # A query token must never follow an unexpected redirect.


class Client:
    def __init__(self, account, *, opener=None):
        self.auth = credentials(account['email'], account['pickup'])
        # Standard TLS verification and inherited proxy/CA configuration remain enabled.
        self.opener = opener or build_opener(_NoRedirects())

    def _get(self, path, timeout=15):
        query = urlencode({'email': self.auth.email, 'token': self.auth.token})
        req = Request(f'{BASE_URL}{path}?{query}', headers={'Accept': 'application/json'})
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise MailError('Remail response exceeded size limit')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise MailError('Unexpected Remail response schema')
            return result
        except HTTPError as exc:
            delay = 0
            retry = exc.headers.get('Retry-After', '') if exc.headers else ''
            try:
                delay = float(retry)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(retry).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    pass
            if not math.isfinite(delay):
                delay = 0
            status = exc.code
            exc.close()
            raise MailError(f'Remail HTTP {status}', retryable=status in (429, 500, 502, 503, 504),
                            retry_after=max(0, delay)) from None
        except (URLError, OSError, ValueError):
            raise MailError('Remail transport or JSON error', retryable=True) from None

    def pickup(self, timeout=15):
        result = self._get('/v1/pickup', timeout)
        items = result.get('items')
        fetch = result.get('fetch')
        if (not isinstance(items, list) or not isinstance(fetch, dict)
                or any(not isinstance(m, dict) or type(m.get('id')) is not int or m['id'] <= 0
                       for m in items)):
            raise MailError('Unexpected Remail pickup schema')
        return items, fetch

    def message(self, message_id, timeout=15):
        if type(message_id) is not int or message_id <= 0:
            raise MailError('Invalid message ID')
        return self._get(f'/v1/pickup/messages/{message_id}', timeout)


@dataclass(frozen=True)
class Snapshot:
    ids: frozenset
    since: float
    next_fetch: float


class Mailbox:
    def __init__(self, client, allowed_senders, *, poll_seconds=5,
                 clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.client = client
        self.senders = frozenset(s.casefold() for s in allowed_senders)
        if not self.senders or any('@' not in s for s in self.senders):
            raise MailError('Configure exact trusted Claude sender addresses before issuing codes')
        self.poll_seconds = max(1, float(poll_seconds))
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep

    def snapshot(self):
        items, fetch = self.client.pickup()
        return Snapshot(frozenset(m['id'] for m in items), self.clock(),
                        timestamp(fetch.get('nextFetchAllowedAt')) or 0)

    def _matches(self, message, snapshot):
        received = timestamp(message.get('receivedAt'))
        sender = parseaddr(str(message.get('sender', '')))[1].casefold()
        recipient = parseaddr(str(message.get('recipient', '')))[1]
        return (message.get('id') not in snapshot.ids and received is not None
                and snapshot.since <= received <= self.clock()
                and sender in self.senders and recipient == self.client.auth.email)

    def wait(self, snapshot, timeout=120):
        if not 0 < timeout <= 600:
            raise MailError('Mail wait timeout must be between 0 and 600 seconds')
        deadline = self.monotonic() + timeout
        next_poll = snapshot.next_fetch
        while self.monotonic() < deadline:
            delay = max(0, next_poll - self.clock())
            self.sleep(min(delay, max(0, deadline - self.monotonic())))
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            try:
                items, fetch = self.client.pickup(timeout=min(15, remaining))
                next_poll = max(self.clock() + self.poll_seconds,
                                timestamp(fetch.get('nextFetchAllowedAt')) or 0)
                matching = [m for m in items if self._matches(m, snapshot)]
                # Multiple login attempts cannot be correlated to one buyer: fail closed.
                if len(matching) > 1:
                    raise MailError('Multiple fresh messages; operator review required')
                if matching:
                    m = matching[0]
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        break
                    detail = self.client.message(m['id'], timeout=min(15, remaining))
                    if detail.get('id') != m['id'] or not self._matches(detail, snapshot):
                        raise MailError('Message details do not match the requested mailbox')
                    if self.monotonic() >= deadline:
                        break
                    return {'value': extract_login(detail), 'message_id': m['id']}
            except MailError as exc:
                if not exc.retryable:
                    raise
                next_poll = max(next_poll, self.clock() + max(self.poll_seconds, exc.retry_after))
        raise MailError('Timed out waiting for a fresh login email')


class Provider:
    """Compose the mail client with an independently validated Claude adapter."""
    def __init__(self, claude, settings, client_factory=Client):
        self.claude, self.settings, self.client_factory = claude, settings, client_factory

    def usage(self, account):
        return self.claude.usage(account)

    def revoke_all(self, rental):
        return self.claude.revoke_all(rental)

    def issue(self, account, order_id):
        mailbox = Mailbox(self.client_factory(account), self.settings['allowed_senders'],
                          poll_seconds=self.settings.get('poll_seconds', 5))
        snapshot = mailbox.snapshot()
        # Caller exclusively reserves the account before this method; never retry login here.
        self.claude.request_login(account, order_id)
        return mailbox.wait(snapshot, self.settings.get('timeout_seconds', 120))


def main():
    import argparse
    from rental import Store
    p = argparse.ArgumentParser(description='Check Remail access without exposing codes or tokens')
    p.add_argument('--db', default='data/rentals.sqlite3')
    p.add_argument('--account-id', type=int, required=True)
    p.add_argument('--show-senders', action='store_true', help='List sender addresses, without mail bodies or codes')
    args = p.parse_args()
    account = next((a for a in Store(args.db).accounts() if a['id'] == args.account_id), None)
    if account is None:
        p.exit(1, 'Account not found\n')
    try:
        items, _ = Client(account).pickup()
    except MailError as exc:
        p.exit(1, f'{exc}\n')
    print(f'Remail API доступен. Писем в ответе: {len(items)}. Коды и содержимое скрыты.')
    if args.show_senders:
        senders = sorted({parseaddr(str(m.get('sender', '')))[1] for m in items})
        print(json.dumps({'senders': senders}, ensure_ascii=True))


if __name__ == '__main__':
    main()
