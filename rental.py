# noplug
"""Durable exclusive rentals. Provider operations must be idempotent by order ID."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from remail import credentials, login_value


class Unavailable(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.db() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                    pickup TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'quarantine',
                    five REAL, seven REAL, checked REAL);
                CREATE TABLE IF NOT EXISTS rentals (
                    order_id TEXT PRIMARY KEY, buyer TEXT NOT NULL, chat TEXT NOT NULL,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    seconds INTEGER NOT NULL, state TEXT NOT NULL,
                    expires REAL NOT NULL, code_hash TEXT UNIQUE,
                    FOREIGN KEY(account_id) REFERENCES accounts(id));
                CREATE UNIQUE INDEX IF NOT EXISTS exclusive_account
                    ON rentals(account_id) WHERE state != 'closed';
                CREATE TABLE IF NOT EXISTS mail_claims (
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    message_id INTEGER NOT NULL,
                    order_id TEXT NOT NULL UNIQUE REFERENCES rentals(order_id),
                    PRIMARY KEY(account_id, message_id));
            ''')
            if 'disabled' not in {r[1] for r in c.execute('PRAGMA table_info(accounts)')}:
                c.execute('ALTER TABLE accounts ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('BEGIN IMMEDIATE')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def add(self, email, pickup):
        auth = credentials(email, pickup)
        with self.db() as c:
            c.execute('INSERT INTO accounts(email,pickup) VALUES (?,?)', (email, auth.pickup_url()))

    def usage(self, account_id, five, seven, now=None):
        now = time.time() if now is None else now
        if any(isinstance(x, bool) or not isinstance(x, (float, int)) or
               not math.isfinite(x) or not 0 <= x <= 100 for x in (five, seven)):
            raise ValueError('Usage must contain two percentages from 0 to 100')
        with self.db() as c:
            c.execute("UPDATE accounts SET five=?,seven=?,checked=?,state=CASE WHEN state='quarantine' THEN state ELSE ? END WHERE id=?",
                      (five, seven, now, 'hold' if max(five, seven) > 70 else 'ready', account_id))

    def hold(self, account_id):
        with self.db() as c:
            c.execute("UPDATE accounts SET state=CASE WHEN state='quarantine' THEN state ELSE 'hold' END,checked=NULL WHERE id=?", (account_id,))

    def initialized(self, account_id):
        with self.db() as c:
            c.execute("UPDATE accounts SET state='hold',checked=NULL WHERE id=? AND state='quarantine'", (account_id,))

    def reserve(self, order_id, buyer, chat, seconds, now=None):
        now = time.time() if now is None else now
        if type(seconds) is not int or not 0 < seconds <= 31 * 86400:
            raise ValueError('Invalid rental duration')
        with self.db() as c:
            old = c.execute('SELECT * FROM rentals WHERE order_id=?', (order_id,)).fetchone()
            if old:
                if old['buyer'] != str(buyer) or old['chat'] != str(chat):
                    raise ValueError('Order owner mismatch')
                return None  # Duplicate events never issue another login code.
            a = c.execute('''SELECT * FROM accounts a WHERE state='ready' AND disabled=0
                AND checked BETWEEN ? AND ? AND five<=70 AND seven<=70
                AND NOT EXISTS (SELECT 1 FROM rentals r WHERE r.account_id=a.id AND r.state!='closed')
                ORDER BY MAX(five,seven),id LIMIT 1''', (now - 120, now)).fetchone()
            if not a:
                raise Unavailable('No account with fresh usage and confirmed session cleanup')
            c.execute('INSERT INTO rentals VALUES (?,?,?,?,?,?,?,?)',
                      (str(order_id), str(buyer), str(chat), a['id'], seconds,
                       'issuing', now + seconds, None))
            return dict(a)

    def claim_code(self, order_id, code):
        message_id = None
        if isinstance(code, dict):
            message_id = code.get('message_id')
            code = code.get('value')
            if type(message_id) is not int or message_id <= 0:
                raise ValueError('Invalid mail message ID')
        code = login_value(code)
        if code.isdigit() and message_id is None:
            raise ValueError('Numeric codes require a mail message ID')
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.db() as c:
            if message_id is not None:
                r = c.execute('SELECT account_id FROM rentals WHERE order_id=?', (order_id,)).fetchone()
                if r is None:
                    raise Unavailable('Order not found')
                c.execute('INSERT INTO mail_claims VALUES (?,?,?)', (r[0], message_id, order_id))
            result = c.execute("UPDATE rentals SET code_hash=? WHERE order_id=? AND state='issuing' AND code_hash IS NULL",
                               (digest, order_id))
            if result.rowcount != 1:
                raise Unavailable('Code already claimed')
        return code

    def active(self, order_id, now=None):
        now = time.time() if now is None else now
        with self.db() as c:
            c.execute("UPDATE rentals SET state='active',expires=?+seconds WHERE order_id=? AND state='issuing'",
                      (now, order_id))

    def pending_cleanup(self, now=None):
        now = time.time() if now is None else now
        with self.db() as c:
            c.execute("UPDATE rentals SET state='cleanup' WHERE state IN ('active','issuing') AND expires<=?", (now,))
            return [dict(r) for r in c.execute('''SELECT r.*,a.email,a.pickup FROM rentals r
                JOIN accounts a ON a.id=r.account_id WHERE r.state='cleanup' ''')]

    def close(self, order_id):
        with self.db() as c:
            r = c.execute('SELECT account_id FROM rentals WHERE order_id=?', (order_id,)).fetchone()
            c.execute("UPDATE accounts SET state='hold',checked=NULL WHERE id=?", (r[0],))
            c.execute("UPDATE rentals SET state='closed' WHERE order_id=? AND state='cleanup'", (order_id,))

    def accounts(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM accounts')]

    def set_disabled(self, account_id, disabled):
        with self.db() as c:
            result = c.execute('UPDATE accounts SET disabled=? WHERE id=?', (int(disabled), account_id))
            if result.rowcount != 1:
                raise ValueError('Account not found')

    def admin_accounts(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('''SELECT a.id,a.email,a.state,a.disabled,a.five,a.seven,
                r.order_id,r.state AS rental_state,r.expires FROM accounts a LEFT JOIN rentals r
                ON a.id=r.account_id AND r.state!='closed' ORDER BY a.id''')]

    def recent_rentals(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('''SELECT order_id,account_id,state,expires
                FROM rentals ORDER BY rowid DESC LIMIT 20''')]


class Engine:
    """Provider contract: usage, issue, revoke_all. No simulated success in production."""
    def __init__(self, store, provider, send):
        self.store, self.provider, self.send = store, provider, send

    def tick(self):
        for r in self.store.pending_cleanup():
            try:
                # Must verify remote invalidation, including any reusable login link.
                if self.provider.revoke_all(r) is True:
                    self.store.close(r['order_id'])
            except Exception:
                pass  # Keep exclusive reservation; never free on a network error.
        for a in self.store.accounts():
            try:
                if a['state'] == 'quarantine':
                    if self.provider.revoke_all({**a, 'account_id': a['id'],
                                                 'order_id': f"initialize-{a['id']}"}) is not True:
                        continue
                    self.store.initialized(a['id'])
                five, seven = self.provider.usage(a)
                self.store.usage(a['id'], five, seven)
            except Exception:
                self.store.hold(a['id'])

    def deliver(self, order_id, buyer, chat, seconds):
        a = self.store.reserve(order_id, buyer, chat, seconds)
        if a is None:
            return False
        # On exception or crash leave issuing until expiry and verified cleanup.
        code = self.store.claim_code(order_id, self.provider.issue(a, order_id))
        if not self.send(chat, f"Аренда Claude: {seconds // 3600} ч.\nПочта: {a['email']}\nВход: {code}"):
            raise Unavailable('Delivery unconfirmed; account remains reserved')
        self.store.active(order_id)
        return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--db', default='data/rentals.sqlite3')
    p.add_argument('command', choices=['add', 'status'])
    args = p.parse_args()
    s = Store(args.db)
    if args.command == 'add':
        import getpass
        s.add(input('Email: ').strip(), getpass.getpass('Pickup URL (hidden): ').strip())
    else:
        # Pickup URLs, emails and login codes are deliberately omitted.
        print(json.dumps([{k: a[k] for k in ('id', 'state', 'five', 'seven', 'checked')}
                          for a in s.accounts()], indent=2))


if __name__ == '__main__':
    main()
