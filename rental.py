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
from datetime import datetime, timedelta, timezone
from remail import credentials, login_value


class Unavailable(Exception):
    pass


def format_duration(seconds):
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return ' '.join(f'{v} {unit}' for v, unit in ((hours, 'ч'), (minutes, 'мин'), (seconds, 'сек')) if v)


def end_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=3))).strftime('%d.%m %H:%M МСК')


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
                CREATE TABLE IF NOT EXISTS purchases (
                    order_id TEXT PRIMARY KEY, buyer TEXT NOT NULL, chat TEXT NOT NULL,
                    seconds INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'pending',
                    rental_id TEXT REFERENCES rentals(order_id), created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auto_kicks (
                    order_id TEXT PRIMARY KEY REFERENCES rentals(order_id),
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    verified_at REAL);
                CREATE TABLE IF NOT EXISTS buyers (
                    buyer TEXT PRIMARY KEY, first_order_id TEXT NOT NULL UNIQUE,
                    bonus_state TEXT NOT NULL DEFAULT 'pending');
            ''')
            if 'disabled' not in {r[1] for r in c.execute('PRAGMA table_info(accounts)')}:
                c.execute('ALTER TABLE accounts ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0')
            if 'reminder_expires' not in {r[1] for r in c.execute('PRAGMA table_info(rentals)')}:
                c.execute('ALTER TABLE rentals ADD COLUMN reminder_expires REAL')
            if 'bonus_deadline' not in {r[1] for r in c.execute('PRAGMA table_info(rentals)')}:
                c.execute('ALTER TABLE rentals ADD COLUMN bonus_deadline REAL')
                c.execute('UPDATE rentals SET bonus_deadline=expires')
            c.execute('''INSERT OR IGNORE INTO purchases(order_id,buyer,chat,seconds,kind,rental_id,created)
                SELECT order_id,buyer,chat,seconds,'initial',order_id,0 FROM rentals ORDER BY rowid''')
            c.execute('''INSERT OR IGNORE INTO buyers(buyer,first_order_id)
                SELECT buyer,order_id FROM purchases ORDER BY rowid''')

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

    @staticmethod
    def _record(c, order_id, buyer, chat, seconds, now):
        if type(seconds) is not int or not 0 < seconds <= 31 * 86400:
            raise ValueError('Invalid rental duration')
        order_id, buyer, chat = str(order_id), str(buyer), str(chat)
        c.execute('INSERT OR IGNORE INTO purchases(order_id,buyer,chat,seconds,created) VALUES (?,?,?,?,?)',
                  (order_id, buyer, chat, seconds, now))
        p = c.execute('SELECT * FROM purchases WHERE order_id=?', (order_id,)).fetchone()
        if (p['buyer'], p['chat'], p['seconds']) != (buyer, chat, seconds):
            raise ValueError('Order parameters mismatch')
        c.execute('INSERT OR IGNORE INTO buyers(buyer,first_order_id) VALUES (?,?)', (buyer, order_id))
        return dict(p)

    def record_purchase(self, order_id, buyer, chat, seconds, now=None):
        with self.db() as c:
            return self._record(c, order_id, buyer, chat, seconds, time.time() if now is None else now)

    def renew(self, order_id, buyer, chat, seconds, now=None):
        now = time.time() if now is None else now
        with self.db() as c:
            p = self._record(c, order_id, buyer, chat, seconds, now)
            if p['kind'] != 'pending':
                return {'duplicate': True}
            leases = c.execute("SELECT * FROM rentals WHERE buyer=? AND state!='closed' AND expires>?",
                               (str(buyer), now)).fetchall()
            if not leases:
                return None
            if len(leases) != 1 or leases[0]['state'] != 'active':
                raise Unavailable('Previous rental is not ready for renewal')
            r = leases[0]
            expires = r['expires'] + seconds
            c.execute('UPDATE rentals SET expires=?,seconds=seconds+? WHERE order_id=?', (expires, seconds, r['order_id']))
            c.execute("UPDATE purchases SET kind='extension',rental_id=? WHERE order_id=?", (r['order_id'], str(order_id)))
            return {'duplicate': False, 'expires': expires, 'account_id': r['account_id']}

    def is_first_purchase(self, order_id, buyer):
        with self.db() as c:
            r = c.execute('SELECT first_order_id FROM buyers WHERE buyer=?', (str(buyer),)).fetchone()
            return r is not None and r[0] == str(order_id)

    def award_review_bonus(self, order_id, buyer, now=None):
        now = time.time() if now is None else now
        with self.db() as c:
            b = c.execute('SELECT * FROM buyers WHERE buyer=?', (str(buyer),)).fetchone()
            if not b or b['first_order_id'] != str(order_id) or b['bonus_state'] != 'pending':
                return None
            r = c.execute('SELECT * FROM rentals WHERE order_id=? AND buyer=?', (str(order_id), str(buyer))).fetchone()
            if not r or (r['state'] == 'issuing' and r['expires'] > now):
                return None
            eligible = r['state'] == 'active' and min(r['expires'], r['bonus_deadline'] or r['expires']) > now
            state = 'awarded' if eligible else 'too_late'
            c.execute('UPDATE buyers SET bonus_state=? WHERE buyer=?', (state, str(buyer)))
            if eligible:
                c.execute('UPDATE rentals SET expires=expires+3600,seconds=seconds+3600 WHERE order_id=?', (str(order_id),))
            return {'state': state, 'chat': r['chat'], 'expires': r['expires'] + (3600 if eligible else 0)}

    def review_candidates(self):
        with self.db() as c:
            return [r[0] for r in c.execute('''SELECT b.first_order_id FROM buyers b JOIN rentals r
                ON r.order_id=b.first_order_id WHERE b.bonus_state='pending' AND r.state='active'
                AND r.bonus_deadline>? ORDER BY r.rowid''', (time.time(),))]

    def claim_reminders(self, now=None, lead_seconds=900):
        now = time.time() if now is None else now
        with self.db() as c:
            rows = [dict(r) for r in c.execute('''SELECT order_id,chat,expires FROM rentals
                WHERE state='active' AND expires>? AND expires<=?
                AND (reminder_expires IS NULL OR reminder_expires!=expires)''', (now, now + lead_seconds))]
            for r in rows:
                c.execute('UPDATE rentals SET reminder_expires=? WHERE order_id=?', (r['expires'], r['order_id']))
            return rows

    def reserve(self, order_id, buyer, chat, seconds, now=None):
        now = time.time() if now is None else now
        if type(seconds) is not int or not 0 < seconds <= 31 * 86400:
            raise ValueError('Invalid rental duration')
        with self.db() as c:
            p = self._record(c, order_id, buyer, chat, seconds, now)
            old = c.execute('SELECT * FROM rentals WHERE order_id=?', (order_id,)).fetchone()
            if old:
                if old['buyer'] != str(buyer) or old['chat'] != str(chat):
                    raise ValueError('Order owner mismatch')
                return None  # Duplicate events never issue another login code.
            if p['kind'] != 'pending':
                return None
            if c.execute("SELECT 1 FROM rentals WHERE buyer=? AND state!='closed' AND expires>?",
                         (str(buyer), now)).fetchone():
                raise Unavailable('Buyer already has an active rental; renew it instead')
            a = c.execute('''SELECT * FROM accounts a WHERE state='ready' AND disabled=0
                AND checked BETWEEN ? AND ? AND five<=70 AND seven<=70
                AND NOT EXISTS (SELECT 1 FROM rentals r WHERE r.account_id=a.id AND r.state!='closed')
                ORDER BY MAX(five,seven),id LIMIT 1''', (now - 120, now)).fetchone()
            if not a:
                raise Unavailable('No account with fresh usage and confirmed session cleanup')
            c.execute('INSERT INTO rentals(order_id,buyer,chat,account_id,seconds,state,expires,code_hash) VALUES (?,?,?,?,?,?,?,?)',
                      (str(order_id), str(buyer), str(chat), a['id'], seconds,
                       'issuing', now + seconds, None))
            c.execute("UPDATE purchases SET kind='initial',rental_id=? WHERE order_id=?", (str(order_id), str(order_id)))
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
            c.execute("UPDATE rentals SET state='active',expires=?+seconds,bonus_deadline=?+seconds WHERE order_id=? AND state='issuing'",
                      (now, now, order_id))

    def pending_cleanup(self, now=None):
        now = time.time() if now is None else now
        with self.db() as c:
            c.execute("UPDATE rentals SET state='cleanup' WHERE state IN ('active','issuing') AND expires<=?", (now,))
            return [dict(r) for r in c.execute('''SELECT r.*,a.email,a.pickup FROM rentals r
                JOIN accounts a ON a.id=r.account_id WHERE r.state='cleanup' ''')]

    def claim_kick(self, order_id, now=None):
        now = time.time() if now is None else now
        with self.db() as c:
            row = c.execute("SELECT account_id FROM rentals WHERE order_id=? AND state='cleanup' AND expires<=?",
                            (order_id, now)).fetchone()
            if row is None:
                return False
            c.execute("INSERT OR IGNORE INTO auto_kicks(order_id,state) VALUES (?,'pending')", (order_id,))
            updated = c.execute("""UPDATE auto_kicks SET state='working',attempts=attempts+1,
                next_attempt=?,error='' WHERE order_id=? AND state!='browser_revoked' AND next_attempt<=?""",
                (now + 60, order_id, now))
            if updated.rowcount != 1:
                return False
            c.execute("UPDATE accounts SET state='hold',checked=NULL WHERE id=?", (row[0],))
            return True

    def finish_kick(self, order_id, error='', now=None):
        now = time.time() if now is None else now
        with self.db() as c:
            row = c.execute("SELECT attempts FROM auto_kicks WHERE order_id=? AND state='working'", (order_id,)).fetchone()
            if row is None:
                raise Unavailable('No running cleanup')
            delay = min(300, 30 * 2 ** min(row[0], 4))
            c.execute("""UPDATE auto_kicks SET state=?,error=?,next_attempt=?,verified_at=?
                WHERE order_id=?""", ('retry' if error else 'browser_revoked', error,
                                       now + delay if error else 0, None if error else now, order_id))
            # Keep exclusive reservation: browser evidence does not cover Code or login links.

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
            return [dict(r) for r in c.execute('''SELECT r.order_id,r.account_id,r.state,r.expires,r.seconds,
                k.state AS kick_state,k.error AS kick_error,k.attempts AS kick_attempts
                FROM rentals r LEFT JOIN auto_kicks k ON k.order_id=r.order_id ORDER BY r.rowid DESC LIMIT 20''')]


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
        renewal = self.store.renew(order_id, buyer, chat, seconds)
        if renewal is not None:
            if renewal.get('duplicate'):
                return False
            self.notify_renewal(chat, seconds, renewal)
            return True
        a = self.store.reserve(order_id, buyer, chat, seconds)
        if a is None:
            return False
        first = self.store.is_first_purchase(order_id, buyer)
        greeting = (f'Здравствуйте! Спасибо за покупку. Аренда Claude: {format_duration(seconds)}.\n'
                    f'Ваша почта: {a["email"]}\nКод или ссылка входа выдаётся только один раз на эту аренду. '
                    'Сейчас подготовлю вход. При продлении новый код не нужен.')
        if first:
            greeting += ('\nЭто ваша первая покупка: +1 час за отзыв к этому заказу, '
                         'если бот подтвердит его до окончания первоначального срока аренды. Оставьте отзыв сразу; '
                         'позже бонус не начисляется. Продление не увеличивает срок для получения бонуса.')
        else:
            greeting += '\nБонус за отзыв действует только при первой покупке.'
        if not self.send(chat, greeting):
            raise Unavailable('Greeting unconfirmed; account remains reserved')
        # On exception or crash leave issuing until expiry and verified cleanup.
        code = self.store.claim_code(order_id, self.provider.issue(a, order_id))
        if not self.send(chat, f"Вход: {code}\nСрок: {format_duration(seconds)}. Код выдан один раз; повторной выдачи нет."):
            raise Unavailable('Delivery unconfirmed; account remains reserved')
        self.store.active(order_id)
        return True

    def notify_renewal(self, chat, seconds, renewal):
        if not self.send(chat, f'Аренда продлена на {format_duration(seconds)}. Окончание: {end_time(renewal["expires"])}. '
                         'Оставайтесь в том же аккаунте: новый код при продлении не выдаётся.'):
            raise Unavailable('Rental extended, notification unconfirmed')

    def remind(self):
        failed = False
        for r in self.store.claim_reminders():
            with self.store.db() as c:
                current = c.execute("SELECT 1 FROM rentals WHERE order_id=? AND state='active' AND expires=? AND expires>?",
                                    (r['order_id'], r['expires'], time.time())).fetchone()
            if not current:
                continue
            minutes = max(1, math.ceil((r['expires'] - time.time()) / 60))
            try:
                if not self.send(r['chat'], f'Аренда заканчивается примерно через {minutes} мин — {end_time(r["expires"])}. '
                                 'Для продления до окончания аренды купите лот на нужный срок. Время добавится к текущему аккаунту, новый код не нужен.'):
                    failed = True
            except Exception:
                failed = True
        if failed:
            raise Unavailable('Some reminder deliveries were not confirmed')

    def apply_review(self, order_id, buyer):
        result = self.store.award_review_bonus(order_id, buyer)
        if result:
            text = (f'Спасибо за отзыв к первой покупке! Начислен +1 час. Окончание: {end_time(result["expires"])}.'
                    if result['state'] == 'awarded' else
                    'Отзыв подтверждён после первоначального срока первой аренды: бонусный час не начислен. '
                    'Оставлять отзыв нужно сразу; продление не увеличивает срок для бонуса.')
            if not self.send(result['chat'], text):
                raise Unavailable('Review processed, notification unconfirmed')
        return result


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
