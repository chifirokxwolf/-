"""Run against the user's actual Cardinal checkout: set CARDINAL_SOURCE."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = os.environ.get('CARDINAL_SOURCE')
ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(SOURCE, 'Set CARDINAL_SOURCE to run real Cardinal loader/Telegram tests')
class CardinalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, SOURCE)
        from cardinal import Cardinal
        from tg_bot.bot import TGBot
        from telebot import TeleBot, types
        from build_plugin import build
        cls.Cardinal, cls.TGBot, cls.TeleBot, cls.types = Cardinal, TGBot, TeleBot, types
        cls.bundle = build(ROOT)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_cwd = Path.cwd()
        os.chdir(self.tmp.name)
        Path('plugins').mkdir()
        shutil.copy2(self.bundle, 'plugins/claude_rental_plugin.py')
        c = object.__new__(self.Cardinal)
        c.plugins, c.disabled_plugins, c.pinned_plugins = {}, [], []
        c.send_message = Mock(return_value=[object()])
        tg = object.__new__(self.TGBot)
        tg.bot = self.TeleBot('123:local-fixture', parse_mode='HTML', threaded=False)
        tg.bot.send_message = Mock(side_effect=lambda *a, **kw: self.message('', mid=100))
        tg.bot.delete_message = Mock()
        tg.bot.answer_callback_query = Mock()
        tg.bot.set_my_commands = Mock()
        tg.cardinal, tg.user_states, tg.authorized_users, tg.commands = c, {}, {1: {}}, {}
        c.telegram = tg
        c.load_plugins()
        self.assertEqual(len(c.plugins), 1, 'Standalone plugin did not load')
        self.plugin_data = next(iter(c.plugins.values()))
        self.plugin = self.plugin_data.plugin
        c.handler_bind_var_names = {name: [] for name in
            ('BIND_TO_PRE_INIT', 'BIND_TO_POST_START', 'BIND_TO_PRE_STOP', 'BIND_TO_NEW_ORDER')}
        c.add_handlers()
        self.c, self.tg = c, tg
        c.run_handlers(c.handler_bind_var_names['BIND_TO_PRE_INIT'], (c,))
        self.runtime = self.plugin._runtime
        self.assertIsNotNone(self.runtime)

    def tearDown(self):
        if hasattr(self, 'runtime'):
            self.runtime.shutdown()
            for t in self.runtime.threads:
                t.join(timeout=2)
        os.chdir(self.previous_cwd)
        self.tmp.cleanup()

    def message(self, text, user=1, chat_type='private', mid=1):
        return self.types.Message.de_json({'message_id': mid, 'date': 1,
            'from': {'id': user, 'is_bot': False, 'first_name': 'Test'},
            'chat': {'id': user, 'type': chat_type}, 'text': text})

    def dispatch(self, text, user=1, chat_type='private'):
        message = self.message(text, user, chat_type)
        for handler in self.tg.bot.message_handlers:
            if self.tg.bot._test_message_handler(handler, message):
                handler['function'](message)
                return
        self.fail('No message handler matched')

    def click(self, action, user=1, chat_type='private', native=False):
        from tg_bot import CBT
        data = f'{CBT.PLUGIN_SETTINGS}:{self.plugin.UUID}:0' if native else 'clauderental:' + action
        call = self.types.CallbackQuery.de_json({'id': 'callback', 'chat_instance': 'fixture',
            'from': {'id': user, 'is_bot': False, 'first_name': 'Test'}, 'data': data,
            'message': {'message_id': 1, 'date': 1, 'chat': {'id': user, 'type': chat_type}, 'text': 'Menu'}})
        for handler in self.tg.bot.callback_query_handlers:
            if self.tg.bot._test_message_handler(handler, call):
                handler['function'](call)
                return
        self.fail('No callback handler matched')

    def output(self):
        return '\n'.join(str(call.args[1]) for call in self.tg.bot.send_message.call_args_list)

    def add_account(self):
        self.click('add')
        self.dispatch('test@example.org')
        self.dispatch('https://remail.aishop6.com/pickup?email=test%40example.org&token=LOCAL-SECRET')

    def test_native_loader_and_settings_page_without_adapter(self):
        from tg_bot.keyboards import edit_plugin
        self.assertTrue(self.plugin_data.settings_page)
        self.assertTrue(self.Cardinal.is_uuid_valid(self.plugin.UUID))
        self.assertTrue(Path('storage/claude_rental/config.json').is_file())
        self.assertIsNone(self.runtime.engine)
        self.tg.setup_commands()
        self.assertIn('claude_rental', self.tg.commands)
        keyboard = edit_plugin(self.c, self.plugin.UUID, 0).to_dict()
        self.assertTrue(any('47:' + self.plugin.UUID in b.get('callback_data', '')
                            for row in keyboard['inline_keyboard'] for b in row))
        self.click('', native=True)
        self.assertIn('Не подключён Claude-адаптер', self.output())

    def test_add_account_and_check_mail_do_not_echo_secrets(self):
        self.add_account()
        self.assertEqual(len(self.runtime.store.accounts()), 1)
        self.tg.bot.delete_message.assert_called_once()
        self.click('accounts:0')
        self.click('account:1')
        with patch.object(self.runtime.mail, 'Client') as client:
            client.return_value.pickup.return_value = ([{'verificationCode': '998877'}], {})
            self.click('check:1')
        self.assertIn('Писем: 1', self.output())
        self.assertNotIn('LOCAL-SECRET', self.output())
        self.assertNotIn('998877', self.output())
        self.assertIsNone(self.tg.get_state(1, 1))

    def test_add_best_link_and_display_specific_validation_errors(self):
        self.click('add')
        self.dispatch('test@example.org')
        self.dispatch('https://remail.best/pickup?email=wrong%40example.org&token=LOCAL-SECRET')
        self.assertIn('Почта в ссылке не совпадает', self.output())
        self.assertEqual(len(self.runtime.store.accounts()), 0)
        self.dispatch('[Почта](https://remail.best/pickup?email=test%40example.org\\&token=LOCAL-SECRET)')
        account = self.runtime.store.accounts()[0]
        self.assertTrue(account['pickup'].startswith('https://remail.best/pickup?'))
        self.click('add')
        self.dispatch('test@example.org')
        self.dispatch('https://remail.best/pickup?email=test%40example.org&token=LOCAL-SECRET')
        self.assertIn('Эта почта уже добавлена', self.output())
        self.assertNotIn('LOCAL-SECRET', self.output())

    def test_telegram_hidden_hyperlink_is_accepted(self):
        self.click('add')
        self.dispatch('test@example.org')
        message = self.message('Почта')
        message.entities = [self.types.MessageEntity('text_link', 0, 5,
            url='https://remail.best/pickup?email=test%40example.org&token=LOCAL-SECRET')]
        for handler in self.tg.bot.message_handlers:
            if self.tg.bot._test_message_handler(handler, message):
                handler['function'](message)
                break
        self.assertEqual(len(self.runtime.store.accounts()), 1)
        self.assertNotIn('LOCAL-SECRET', self.output())

    def test_unauthorized_and_group_access_are_denied(self):
        self.dispatch('/claude_rental', user=2)
        self.click('add', user=2)
        self.dispatch('/claude_rental', chat_type='group')
        self.click('add', chat_type='group')
        self.assertFalse(self.tg.bot.send_message.called)
        self.assertEqual(self.tg.user_states, {})

    def test_pause_cannot_enable_missing_adapter(self):
        self.click('toggle')
        self.assertFalse(self.runtime.config['enabled'])
        self.assertIn('Нельзя включить', self.output())

    def test_settings_survive_reinitialization_and_cancel(self):
        self.click('hours')
        self.dispatch('12')
        self.click('lot')
        self.dispatch('Claude (аренда) 12 часов')
        self.click('senders')
        self.dispatch('login@claude.example')
        self.click('add')
        self.dispatch('/claude_rental')
        self.assertIsNone(self.tg.get_state(1, 1))
        other = self.plugin.Runtime(self.c, self.runtime.path)
        self.assertEqual(other.config['rules'][0]['hours'], 12)
        self.assertEqual(self.plugin.duration('Claude (аренда) 12 часов', 1, other.config['rules']), 43200)
        self.assertEqual(other.config['mailbox']['allowed_senders'], ['login@claude.example'])

    def test_manual_exclusion_can_be_reversed_without_freeing_an_active_rental(self):
        self.add_account()
        self.runtime.store.initialized(1)
        self.runtime.store.usage(1, 0, 0)
        self.runtime.store.reserve('paid-order', 'buyer', 'chat', 18000)
        self.click('disable:1')
        self.assertEqual(self.runtime.store.accounts()[0]['disabled'], 1)
        self.click('disable:1')
        self.assertEqual(self.runtime.store.admin_accounts()[0]['order_id'], 'paid-order')
        self.click('rentals')
        self.assertIn('paid-order', self.output())

    def test_real_order_event_enqueues_once_without_blocking_on_mail(self):
        from FunPayAPI.types import OrderShortcut
        from FunPayAPI.common.enums import OrderStatuses, Currency
        from FunPayAPI.updater.events import NewOrderEvent
        order = OrderShortcut('ABCDE', 'Claude PRO НА 5 ЧАСОВ АРЕНДА', 100,
                              Currency.RUB, 'buyer', 7, 9, OrderStatuses.PAID,
                              datetime.now(), 'Claude', None, '')
        self.runtime.engine = Mock()
        self.runtime.config['enabled'] = True
        event = NewOrderEvent('fixture', order)
        for _ in range(2):
            self.c.run_handlers(self.c.handler_bind_var_names['BIND_TO_NEW_ORDER'], (self.c, event))
        self.assertEqual(self.runtime.orders.qsize(), 1)
        self.runtime.engine.deliver.assert_not_called()
        task = self.runtime.orders.get_nowait()
        self.runtime.process_one(task)
        self.runtime.engine.deliver.assert_called_once_with('ABCDE', '7', '9', 18000)
        self.assertEqual(self.runtime.queued, set())
        self.runtime.engine.reset_mock()
        self.plugin_data.enabled = False
        self.runtime.enqueue(order)
        self.assertTrue(self.runtime.orders.empty())

    def test_native_start_stop_and_delete_hooks(self):
        self.c.run_handlers(self.c.handler_bind_var_names['BIND_TO_POST_START'], (self.c,))
        self.assertEqual(len(self.runtime.threads), 2)
        self.c.run_handlers(self.c.handler_bind_var_names['BIND_TO_PRE_STOP'], (self.c,))
        self.assertTrue(self.runtime.stop.is_set())
        self.plugin.BIND_TO_DELETE(self.c, None)

    def test_start_after_stop_uses_new_worker_generation(self):
        self.runtime.start()
        previous = self.runtime.stop
        previous_threads = list(self.runtime.threads)
        self.runtime.shutdown()
        self.runtime.start()
        self.assertTrue(previous.is_set())
        self.assertFalse(self.runtime.stop.is_set())
        self.assertIsNot(previous, self.runtime.stop)
        for thread in previous_threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
