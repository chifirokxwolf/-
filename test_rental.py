import concurrent.futures
from pathlib import Path
import tempfile
import time
import unittest

from rental import Engine, Store, Unavailable
from claude_rental_plugin import duration


class Provider:
    def usage(self, account):
        return (10, 20)

    def issue(self, account, order_id):
        return 'https://claude.ai/magic?token=test'

    def revoke_all(self, rental):
        return False


class RentalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'rentals.sqlite3')
        self.store.add('test@example.org', 'https://remail.aishop6.com/pickup?email=test%40example.org&token=fixture')
        self.store.initialized(1)
        self.store.usage(1, 10, 20)

    def test_concurrent_orders_have_one_winner(self):
        def reserve(n):
            try:
                return self.store.reserve(str(n), str(n), str(n), 18000) is not None
            except Unavailable:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, range(20))), 1)

    def test_duplicate_event_does_not_send_twice_after_restart(self):
        messages = []
        engine = Engine(self.store, Provider(), lambda *args: messages.append(args) or True)
        self.assertTrue(engine.deliver('a', 'buyer', 'chat', 18000))
        restarted = Engine(Store(self.store.path), Provider(), engine.send)
        self.assertFalse(restarted.deliver('a', 'buyer', 'chat', 18000))
        self.assertEqual(len(messages), 1)
        with self.assertRaises(ValueError):
            restarted.deliver('a', 'other', 'chat', 18000)

    def test_both_limits_and_staleness(self):
        for five, seven, checked in [(71, 0, time.time()), (0, 71, time.time()),
                                     (0, 0, time.time() - 121)]:
            self.store.usage(1, five, seven, checked)
            with self.assertRaises(Unavailable):
                self.store.reserve('a', 'b', 'c', 18000)
        self.store.usage(1, 70, 70)
        self.assertIsNotNone(self.store.reserve('a', 'b', 'c', 18000))

    def test_unknown_usage_is_not_available(self):
        class Broken(Provider):
            def usage(self, account):
                raise OSError('network')
        Engine(self.store, Broken(), lambda *a: True).tick()
        with self.assertRaises(Unavailable):
            self.store.reserve('a', 'b', 'c', 18000)

    def test_expired_rental_requires_confirmed_cleanup(self):
        self.store.usage(1, 0, 0, now=time.time() - 3)
        self.store.reserve('a', 'b', 'c', 1, now=time.time() - 2)
        provider = Provider()
        engine = Engine(self.store, provider, lambda *a: True)
        engine.tick()
        with self.assertRaises(Unavailable):
            self.store.reserve('next', 'b', 'c', 18000)
        provider.revoke_all = lambda rental: True
        engine.tick()
        self.assertIsNotNone(self.store.reserve('next', 'b', 'c', 18000))

    def test_same_code_cannot_be_used_for_two_orders(self):
        self.store.usage(1, 0, 0, now=time.time() - 3)
        self.store.reserve('a', 'b', 'c', 1, now=time.time() - 2)
        self.store.claim_code('a', Provider().issue(None, None))
        self.store.pending_cleanup()
        self.store.close('a')
        self.store.usage(1, 0, 0)
        self.store.reserve('next', 'b', 'c', 18000)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.claim_code('next', Provider().issue(None, None))

    def test_unconfirmed_delivery_is_not_retried(self):
        engine = Engine(self.store, Provider(), lambda *a: False)
        with self.assertRaises(Unavailable):
            engine.deliver('a', 'b', 'c', 18000)
        self.assertFalse(engine.deliver('a', 'b', 'c', 18000))
        with self.assertRaises(Unavailable):
            self.store.reserve('next', 'b', 'c', 18000)

    def test_sample_title_does_not_add_review_hour(self):
        import json
        rules = json.loads(Path(__file__).with_name('config.example.json').read_text())['rules']
        title = '【 Claude PRO + ClaudeCode 】👑 НА 5 ЧАСОВ 👑 ✨ +💎1 ЧАС ЗА ОТЗЫВ💎 АРЕНДА 💎'
        self.assertEqual(duration(title, 1, rules), 18000)
        self.assertIsNone(duration('Другой товар', 1, rules))

    def test_mail_id_remains_consumed_after_restart(self):
        import sqlite3
        self.store.usage(1, 0, 0, now=time.time() - 3)
        self.store.reserve('a', 'b', 'c', 1, now=time.time() - 2)
        self.assertEqual(self.store.claim_code('a', {'value': '012345', 'message_id': 42}), '012345')
        self.store.pending_cleanup()
        self.store.close('a')
        restarted = Store(self.store.path)
        restarted.usage(1, 0, 0)
        restarted.reserve('next', 'b', 'c', 18000)
        with self.assertRaises(sqlite3.IntegrityError):
            restarted.claim_code('next', {'value': '654321', 'message_id': 42})
        with restarted.db() as c:
            self.assertIsNone(c.execute("SELECT code_hash FROM rentals WHERE order_id='next'").fetchone()[0])

    def test_mail_numeric_code_delivered_without_token(self):
        provider = Provider()
        provider.issue = lambda *args: {'value': '012345', 'message_id': 42}
        messages = []
        engine = Engine(self.store, provider, lambda *args: messages.append(args) or True)
        self.assertTrue(engine.deliver('a', 'b', 'chat', 18000))
        self.assertEqual(messages[0][0], 'chat')
        self.assertIn('012345', messages[0][1])
        self.assertNotIn('fixture', messages[0][1])
        self.assertNotIn('remail', messages[0][1])

    def test_add_rejects_wrong_mailbox(self):
        from remail import MailError
        with self.assertRaises(MailError):
            self.store.add('other@example.org', 'https://remail.aishop6.com/pickup?email=test%40example.org&token=fixture')


if __name__ == '__main__':
    unittest.main()
