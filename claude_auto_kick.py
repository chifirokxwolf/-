# noplug
"""Expiry-only browser logout. A verified browser logout is not proof of Code revocation.

The rental stays in cleanup until a full provider verifies all sold credentials.
No production success is simulated and no private Claude API is assumed.
"""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from claude_browser_actions import logout_everywhere
from claude_browser_ui import ACCOUNT_URL, LABELS, read_organization
from tools.claude_account_check import connect_profile, visit_settings
from tools.claude_manual_login import installed_browsers, profile_path


class KickError(Exception):
    pass


@contextmanager
def exclusive_worker(path):
    """OS lock survives neither crashes nor process exits; never steal a live job."""
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
        yield locked
    finally:
        if locked:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def logged_out(page):
    u = urlsplit(page.url)
    # A timeout, 403, challenge, blank page or missing settings is NOT success.
    return (u.scheme == 'https' and u.hostname == 'claude.ai' and u.port in (None, 443)
            and not u.username and not u.password and u.path.rstrip('/') == '/login'
            and page.locator('input[type="email"]:visible').count() == 1)


def revoke_in_browser(browser, context, organization, timeout_ms=10000):
    """Observe the old credential in an independent context before and after logout.

    Copy only into memory, never to disk. This detects cookie-only local logout:
    the independent context retains its original cookie. Code and other tokens
    are explicitly outside the evidence produced here.
    """
    page = context.new_page()
    probe_context = None
    try:
        visit_settings(page, ACCOUNT_URL, LABELS['organization'])
        if read_organization(page) != organization:
            raise KickError('account_identity_mismatch')
        probe_context = browser.new_context(storage_state=context.storage_state(), service_workers='block')
        probe = probe_context.new_page()
        visit_settings(probe, ACCOUNT_URL, LABELS['organization'])
        if read_organization(probe) != organization:
            raise KickError('probe_identity_mismatch')
        # Snapshot the *probe's* still-valid credential, including any refresh on load.
        old_state = probe_context.storage_state()

        def verify_revoked(milliseconds):
            # Fresh navigation using the same pre-logout credential on every attempt.
            deadline = time.monotonic() + milliseconds / 1000
            while time.monotonic() < deadline:
                probe_context.clear_cookies()
                probe_context.add_cookies(old_state['cookies'])
                try:
                    response = probe.goto(ACCOUNT_URL, wait_until='domcontentloaded',
                                          timeout=max(100, int((deadline - time.monotonic()) * 1000)))
                    if response is None:
                        # Fragment navigation can reuse the old settings document.
                        # Force a server request before trusting its auth state.
                        response = probe.reload(wait_until='domcontentloaded',
                            timeout=max(100, int((deadline - time.monotonic()) * 1000)))
                    if response is None or response.status >= 400:
                        return False
                    navigation_deadline = min(deadline, time.monotonic() + 2)
                    while time.monotonic() < navigation_deadline:
                        if logged_out(probe):
                            return True
                        # Give the SPA a chance to redirect before reloading.
                        probe.wait_for_timeout(200)
                except Exception:
                    return False
            return False

        return logout_everywhere(page, timeout_ms=timeout_ms,
            verify_account=lambda p: read_organization(p) == organization,
            verify_revoked=verify_revoked)
    finally:
        if probe_context is not None:
            try:
                probe_context.close()
            except Exception:
                pass
        try:
            page.close()
        except Exception:
            pass


class BrowserKicker:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def revoke(self, rental):
        account_id = rental['account_id']
        path = self.root / 'storage/claude_rental/browser_bindings.json'
        if not path.is_file():
            raise KickError('browser_binding_missing')
        binding = json.loads(path.read_text(encoding='utf-8')).get(str(account_id), {})
        # Older check tools didn't bind the email; require one new check after upgrade.
        if binding.get('email', '').casefold() != rental['email'].casefold():
            raise KickError('rerun_account_check')
        organization = binding.get('organization_id', '')
        if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', organization):
            raise KickError('browser_binding_invalid')
        browser_name = binding.get('browser')
        executable = installed_browsers().get(browser_name)
        if executable is None:
            raise KickError('installed_browser_missing')
        profile = profile_path(account_id, browser_name, self.root)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise KickError('playwright_missing') from None
        # Playwright objects are created, used and disposed in this worker only.
        with sync_playwright() as p:
            browser = connect_profile(p, executable, profile)
            if len(browser.contexts) != 1:
                raise KickError('ambiguous_browser_context')
            return revoke_in_browser(browser, browser.contexts[0], organization.lower())


class AutoKick:
    def __init__(self, store, kicker):
        self.store, self.kicker = store, kicker
        self.last_error = ''

    def tick(self, stop=None):
        with exclusive_worker(self.store.path + '.autokick.lock') as acquired:
            if not acquired:
                return
            for rental in self.store.pending_cleanup():
                if stop is not None and stop.is_set():
                    return
                if not self.store.claim_kick(rental['order_id']):
                    continue
                try:
                    if self.kicker.revoke(rental) is not True:
                        raise KickError('revocation_not_confirmed')
                except Exception as exc:
                    known = {'browser_binding_missing', 'rerun_account_check', 'browser_binding_invalid',
                             'installed_browser_missing', 'playwright_missing', 'ambiguous_browser_context',
                             'account_identity_mismatch', 'probe_identity_mismatch', 'revocation_not_confirmed'}
                    code = str(exc) if isinstance(exc, KickError) and str(exc) in known else 'browser_logout_unconfirmed'
                    self.store.finish_kick(rental['order_id'], error=code)
                    self.last_error = code
                else:
                    self.store.finish_kick(rental['order_id'])
                    self.last_error = ''
