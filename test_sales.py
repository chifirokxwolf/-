import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lot_rules import default_rules, duration, parse_minutes, rule_seconds
from rental import Store, Engine, Unavailable

TITLES = [
    ('⚡ KYC CLAUDE PRO + CLAUDE CODE | Аренда 30 минут ⏱️ | Полный Доступ 💎 | OPUS 5.5 🔥', 1800),
    ('⚡ KYC【 Claude PRO + ClaudeCode 】👑 НА 1 ЧАС 👑 ✨ + ЧАС💎 ЧАС ЗА ОТЗЫВ💎 АРЕНДА 💎', 3600),
    ('⚡ KYC【 Claude PRO + ClaudeCode 】👑 НА 2ЧАСА 👑 ✨ + ЧАС💎 ЧАС ЗА ОТЗЫВ💎 АРЕНДА 💎', 7200),
    ('⚡ KYC АКЦИЯ ДО УТРА【 Claude PRO + ClaudeCode 】👑 НА 5 ЧАСОВ 👑 ✨ +💎1 ЧАС ЗА ОТЗЫВ💎 АРЕНДА 💎', 18000),
    ('⚡АВТОВЫДАЧА⚡【 Claude PRO + ClaudeCode 】👑 НА 3 ЧАСА 👑 ✨ЛИЧНЫЙ Аккаунт✨ +💎1 ЧАС ЗА ОТЗЫВ💎 АРЕНДА 💎', 10800),
]


class DurationTests(unittest.TestCase):
    def test_five_real_titles_with_bonus_and_model_numbers(self):
        for title, seconds in TITLES:
            with self.subTest(title=title):
                self.assertEqual(duration(title, 1, default_rules()), seconds)
                self.assertEqual(duration(title, 2, default_rules()), seconds * 2)

    def test_ambiguous_and_unknown_durations(self):
        for title in ['Claude аренда на 1 час или на 2 часа', 'Claude аренда на 5 часов или на 7 часов']:
            with self.assertRaises(ValueError):
                duration(title, 1, default_rules())
        self.assertIsNone(duration('Claude аренда +1 час за отзыв', 1, default_rules()))
        self.assertIsNone(duration('Другой товар на 5 часов аренда', 1, default_rules()))
        self.assertIsNone(duration('Claude аренда на 15 часов', 1, default_rules()))

    def test_manual_minutes_and_legacy_fractional_hours_are_exact(self):
        self.assertEqual(parse_minutes('30 мин'), 30)
        self.assertEqual(parse_minutes('2 часа'), 120)
        self.assertEqual(parse_minutes('90'), 90)
        self.assertEqual(rule_seconds({'hours': 0.5}), 1800)
        with self.assertRaises(ValueError):
            duration(TITLES[1][0] + ', 2 шт.', 1, default_rules())
        for rule in [{'hours': -1}, {'minutes': True}, {'hours': 1, 'minutes': 1}, {'hours': 'NaN'}]:
            with self.assertRaises(ValueError):
                rule_seconds(rule)


class SalesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000
        clock = patch('rental.time.time', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.store = Store(Path(self.tmp.name) / 'rentals.sqlite3')
        for i in (1, 2):
            email = f'test{i}@example.org'
            self.store.add(email, f'https://remail.best/pickup?email={email}&token=fixture')
            self.store.initialized(i)
            self.store.usage(i, 0, 0)
        self.messages = []
        self.issue_calls = []
        class Provider:
            def usage(self, account):
                return 0, 0
            def revoke_all(self, account):
                return False
        self.provider = Provider()
        self.provider.issue = self.issue
        self.engine = Engine(self.store, self.provider, self.send)

    def issue(self, account, order):
        self.issue_calls.append(order)
        return f'https://claude.ai/magic?token=fixture-{order}'

    def send(self, chat, text):
        self.messages.append((chat, text))
        return True

    def lease(self, order='first'):
        with self.store.db() as c:
            return dict(c.execute('SELECT * FROM rentals WHERE order_id=?', (order,)).fetchone())

    def test_greeting_then_one_code_and_exact_30_minute_timer(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        self.assertEqual(self.lease()['expires'], 2800)
        self.assertEqual(self.lease()['seconds'], 1800)
        self.assertIn('test1@example.org', self.messages[0][1])
        self.assertIn('до окончания', self.messages[0][1])
        self.assertNotIn('fixture-first', self.messages[0][1])
        self.assertIn('Вход:', self.messages[1][1])
        self.assertIn('30 мин', self.messages[1][1])
        self.assertNotIn('0 ч.', self.messages[1][1])

    def test_timer_begins_after_confirmed_delivery(self):
        def send(chat, text):
            if text.startswith('Вход:'):
                self.now += 20
            return True
        self.engine.send = send
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        self.assertEqual(self.lease()['expires'], 2820)

    def test_renewal_adds_to_deadline_without_new_account_or_code(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        self.now += 300
        self.store.usage(1, 90, 90)  # Existing paid access can be extended; no fresh account is allocated.
        self.engine.deliver('renewal', 'buyer', 'chat', 7200)
        self.assertEqual(self.lease()['expires'], 10000)
        self.assertEqual(self.issue_calls, ['first'])
        self.assertEqual(self.lease()['account_id'], 1)
        self.assertIn('продлена на 2 ч', self.messages[-1][1])
        restarted = Engine(Store(self.store.path), self.provider, self.send)
        self.assertFalse(restarted.deliver('renewal', 'buyer', 'chat', 7200))
        self.assertEqual(self.lease()['expires'], 10000)

    def test_duplicate_extension_race_adds_time_once(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        def extend(_):
            return self.store.renew('renewal', 'buyer', 'chat', 3600)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(extend, range(12)))
        self.assertEqual(sum(not r['duplicate'] for r in results), 1)
        self.assertEqual(self.lease()['expires'], 6400)

    def test_review_bonus_once_only_for_first_purchase(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        self.engine.deliver('renewal', 'buyer', 'chat', 3600)
        self.assertIsNone(self.engine.apply_review('renewal', 'buyer'))
        self.assertIsNone(self.engine.apply_review('first', 'other-buyer'))
        self.assertEqual(self.engine.apply_review('first', 'buyer')['state'], 'awarded')
        self.assertEqual(self.lease()['expires'], 10000)
        restarted = Engine(Store(self.store.path), self.provider, self.send)
        self.assertIsNone(restarted.apply_review('first', 'buyer'))
        self.assertEqual(self.lease()['expires'], 10000)

    def test_concurrent_reviews_cannot_duplicate_bonus(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.award_review_bonus('first', 'buyer'), range(12)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.lease()['expires'], 6400)

    def test_late_review_does_not_grant_or_carry_bonus(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        self.now = 2800
        self.assertEqual(self.engine.apply_review('first', 'buyer')['state'], 'too_late')
        self.assertIn('не начислен', self.messages[-1][1])
        self.assertEqual(self.lease()['expires'], 2800)
        self.store.usage(2, 0, 0)
        self.engine.deliver('second', 'buyer', 'chat', 1800)
        self.assertEqual(self.lease('second')['expires'], 4600)
        self.assertIsNone(self.engine.apply_review('second', 'buyer'))
        self.assertIsNone(self.engine.apply_review('first', 'buyer'))

    def test_renewal_does_not_extend_review_bonus_deadline(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        self.engine.deliver('renewal', 'buyer', 'chat', 3600)
        self.now = 2801
        self.assertEqual(self.engine.apply_review('first', 'buyer')['state'], 'too_late')
        self.assertEqual(self.lease()['expires'], 6400)

    def test_failed_renewal_notice_does_not_undo_or_repeat_paid_time(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        self.engine.send = lambda *a: False
        with self.assertRaises(Unavailable):
            self.engine.deliver('renewal', 'buyer', 'chat', 3600)
        self.assertEqual(self.lease()['expires'], 6400)
        self.assertFalse(self.engine.deliver('renewal', 'buyer', 'chat', 3600))
        self.assertEqual(self.lease()['expires'], 6400)

    def test_failed_reminder_does_not_prevent_other_buyers_notification(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        self.engine.deliver('other-order', 'other-buyer', 'other-chat', 1800)
        calls = []
        self.engine.send = lambda chat, text: calls.append(chat) or chat == 'other-chat'
        self.now = 1900
        with self.assertRaises(Unavailable):
            self.engine.remind()
        self.assertEqual(set(calls), {'chat', 'other-chat'})

    def test_reminder_persists_and_is_rescheduled_after_renewal(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        self.now = 1899
        self.engine.remind()
        self.assertEqual(len(self.messages), 2)
        self.now = 1900
        self.engine.remind()
        self.assertIn('15 мин', self.messages[-1][1])
        count = len(self.messages)
        Engine(Store(self.store.path), self.provider, self.send).remind()
        self.assertEqual(len(self.messages), count)
        self.engine.deliver('renewal', 'buyer', 'chat', 1800)
        self.engine.remind()
        self.assertEqual(len(self.messages), count + 1)
        self.now = 3700
        self.engine.remind()
        self.assertEqual(len(self.messages), count + 2)

    def test_changed_order_owner_cannot_extend_another_rental(self):
        self.engine.deliver('first', 'buyer', 'chat', 1800)
        with self.assertRaises(ValueError):
            self.engine.deliver('first', 'other', 'chat', 1800)
        self.assertEqual(self.lease()['expires'], 2800)


if __name__ == '__main__':
    unittest.main()
