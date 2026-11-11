"""Claude Rental plugin for FunPay Cardinal 0.1.17.15.
Source edition uses sibling rental.py/remail.py. dist/ contains a single-file build.
"""
import copy
import importlib.util
import json
import logging
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading
import time
from types import ModuleType

NAME = 'Claude Rental'
VERSION = '0.2.0'
DESCRIPTION = 'Аренда Claude: аккаунты, почта Remail и управление выдачей. Требуется Claude-адаптер.'
CREDITS = 'Local development'
UUID = '52280b60-3fd9-4df7-9eb3-f1bb1cd337ef'
SETTINGS_PAGE = True
log = logging.getLogger('FPC.claude_rental')
_runtime = None
DEFAULT_CONFIG = {
    'database': 'storage/claude_rental/rentals.sqlite3',
    'claude_provider_file': '',
    'enabled': False,
    'mailbox': {'allowed_senders': [], 'poll_seconds': 5, 'timeout_seconds': 120},
    'rules': [{'pattern': '(?i)Claude PRO.*НА 5 ЧАСОВ.*АРЕНДА', 'hours': 5,
               'quantity_is_duration': False}],
}


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def components():
    # Release embeds Python sources, never credentials or databases.
    sources = globals().get('_EMBEDDED_SOURCES')
    if sources is None:
        sources = {name: Path(__file__).with_name(name + '.py').read_text(encoding='utf-8')
                   for name in ('remail', 'rental')}
    modules = {}
    for name in ('remail', 'rental'):
        module_name = '_claude_rental_' + name
        module = ModuleType(module_name)
        sys.modules[module_name] = module
        source = sources[name].replace('from remail import credentials, login_value',
                                      'from _claude_rental_remail import credentials, login_value')
        exec(compile(source, f'<claude_rental/{name}.py>', 'exec'), module.__dict__)
        modules[name] = module
    return modules['rental'], modules['remail']


def duration(description, amount, rules):
    matches = [r for r in rules if re.search(r['pattern'], description)]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('Ambiguous rental rule')
    rule = matches[0]
    units = amount if rule.get('quantity_is_duration', False) else 1
    if type(units) is not int or units < 1:
        raise ValueError('Invalid order amount')
    seconds = int(rule['hours']) * 3600 * units
    if not 0 < seconds <= 31 * 86400:
        raise ValueError('Invalid duration')
    return seconds


class Runtime:
    def __init__(self, cardinal, config_path):
        self.cardinal = cardinal
        self.path = Path(config_path)
        self.core, self.mail = components()
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            self.config.update(saved)
            self.config['mailbox'] = {**DEFAULT_CONFIG['mailbox'], **saved.get('mailbox', {})}
        else:
            self.save_config()
        self.store = self.core.Store(self.config['database'])
        self.engine = None
        self.status = 'Не подключён Claude-адаптер. Почта и управление аккаунтами доступны.'
        self.last_error = ''
        self.lock = threading.RLock()
        self.provider_lock = threading.Lock()
        self.stop = threading.Event()
        self.orders = queue.Queue(maxsize=500)
        self.queued = set()
        self.threads = []
        self.connect_provider()

    def save_config(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(prefix='.config-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
                f.write('\n')
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def connect_provider(self):
        path = self.config.get('claude_provider_file') or self.config.get('provider_file')
        if not path or not Path(path).is_file():
            return
        try:
            external = load_file('_claude_rental_provider', path).create(self.config)
            if self.config.get('claude_provider_file'):
                self.mail.Mailbox(None, self.config['mailbox']['allowed_senders'])
                for method in ('usage', 'request_login', 'revoke_all'):
                    if not callable(getattr(external, method, None)):
                        raise ValueError('Invalid Claude provider')
                provider = self.mail.Provider(external, self.config['mailbox'])
            else:
                provider = external
            self.engine = self.core.Engine(self.store, provider, self.send)
            self.status = 'Claude-адаптер загружен; готовность аккаунтов проверяется отдельно.'
        except Exception:
            self.status = 'Не удалось загрузить Claude-адаптер. Проверьте файл и отправителя писем.'

    def send(self, chat, text):
        return bool(self.cardinal.send_message(chat, text, attempts=1, watermark=False))

    def available(self):
        plugin = self.cardinal.plugins.get(UUID)
        return plugin is not None and plugin.enabled

    def enqueue(self, order):
        with self.lock:
            if not self.available() or not self.config['enabled'] or self.engine is None:
                return
            if order.status.name != 'PAID':
                return
            seconds = duration(order.description, order.amount, self.config['rules'])
            if seconds is None or str(order.id) in self.queued:
                return
            task = (str(order.id), str(order.buyer_id), str(order.chat_id), seconds)
            self.orders.put_nowait(task)
            self.queued.add(task[0])

    def process_one(self, task, stop_event=None):
        stop_event = self.stop if stop_event is None else stop_event
        try:
            with self.provider_lock:
                if not stop_event.is_set() and self.available() and self.config['enabled'] and self.engine:
                    self.engine.deliver(*task)
        except Exception:
            self.last_error = f'Заказ {task[0]} требует проверки оператором; повторная выдача не выполняется.'
            log.error('Claude Rental: order requires operator review')
        finally:
            with self.lock:
                self.queued.discard(task[0])

    def start(self):
        if not self.stop.is_set() and any(t.is_alive() for t in self.threads):
            return
        self.stop = run_stop = threading.Event()

        def delivery_worker():
            while not run_stop.is_set():
                try:
                    task = self.orders.get(timeout=1)
                except queue.Empty:
                    continue
                self.process_one(task, run_stop)
                self.orders.task_done()

        def maintenance_worker():
            while not run_stop.is_set():
                if self.engine and self.available():
                    try:
                        with self.provider_lock:
                            if not run_stop.is_set() and self.available():
                                self.engine.tick()
                    except Exception:
                        self.last_error = 'Ошибка фоновой проверки; аккаунты требуют проверки.'
                        log.error('Claude Rental: maintenance failed')
                run_stop.wait(30)

        self.threads = [threading.Thread(target=f, daemon=True, name='claude-rental-' + label)
                        for f, label in ((delivery_worker, 'orders'), (maintenance_worker, 'maintenance'))]
        for thread in self.threads:
            thread.start()

    def shutdown(self):
        self.stop.set()


def register_panel(runtime):
    from html import escape
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    from tg_bot import CBT

    cardinal = runtime.cardinal
    tg = cardinal.telegram
    if not tg:
        return
    bot = tg.bot
    prefix = 'clauderental:'
    state_prefix = 'claude_rental_'

    def permitted(user_id, chat):
        return user_id in tg.authorized_users and chat.type == 'private' and runtime.available()

    def say(chat_id, text, keyboard=None):
        return bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode='HTML')

    def menu(chat_id):
        state = 'включены' if runtime.config['enabled'] else 'на паузе'
        text = (f'<b>Claude Rental {VERSION}</b>\nНовые выдачи: {state}.\n'
                f'{escape(runtime.status)}\nАккаунтов: {len(runtime.store.accounts())}')
        if runtime.last_error:
            text += '\n' + escape(runtime.last_error)
        keyboard = InlineKeyboardMarkup(row_width=2)
        for title, action in [('Аккаунты', 'accounts:0'), ('Добавить аккаунт', 'add'),
                              ('Аренды', 'rentals'), ('Отправители Claude', 'senders'),
                              ('Название лота', 'lot'), ('Срок аренды', 'hours'),
                              ('Пауза / запуск', 'toggle'), ('Обновить', 'menu')]:
            keyboard.add(InlineKeyboardButton(title, callback_data=prefix + action))
        say(chat_id, text, keyboard)

    def command(m):
        if permitted(m.from_user.id, m.chat):
            tg.clear_state(m.chat.id, m.from_user.id)
            menu(m.chat.id)

    def ask(c, state, text, data=None):
        result = say(c.message.chat.id, text + '\nОтмена: /claude_rental')
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, state_prefix + state, data)

    def callback(c):
        if not permitted(c.from_user.id, c.message.chat):
            bot.answer_callback_query(c.id, 'Доступ только администратору в личном чате.')
            return
        bot.answer_callback_query(c.id)
        action = c.data[len(prefix):] if c.data.startswith(prefix) else 'menu'
        chat_id = c.message.chat.id
        try:
            if action == 'menu':
                menu(chat_id)
            elif action == 'add':
                ask(c, 'email', 'Введите точную почту аккаунта Claude.')
            elif action == 'senders':
                current = ', '.join(runtime.config['mailbox']['allowed_senders']) or 'не заданы'
                ask(c, 'senders', f'Сейчас: {escape(current)}\nВведите адреса отправителей писем Claude через запятую.')
            elif action == 'lot':
                ask(c, 'lot', 'Вставьте полное название лота. Правило будет совпадать с ним целиком.')
            elif action == 'hours':
                ask(c, 'hours', f"Сейчас: {runtime.config['rules'][0]['hours']} ч. Введите срок от 1 до 744 часов.")
            elif action == 'toggle':
                with runtime.lock:
                    if not runtime.config['enabled'] and runtime.engine is None:
                        say(chat_id, 'Нельзя включить выдачу: Claude-адаптер не подключён. После его настройки перезапустите Cardinal.')
                        return
                    runtime.config['enabled'] = not runtime.config['enabled']
                    runtime.save_config()
                menu(chat_id)
            elif action.startswith('accounts:'):
                offset = max(0, int(action.split(':')[1]))
                accounts = runtime.store.admin_accounts()
                keyboard = InlineKeyboardMarkup(row_width=1)
                for a in accounts[offset:offset + 8]:
                    state = 'исключён' if a['disabled'] else ('занят' if a['order_id'] else a['state'])
                    keyboard.add(InlineKeyboardButton(f"#{a['id']} {a['email']} — {state}",
                                                     callback_data=prefix + f"account:{a['id']}"))
                if offset:
                    keyboard.add(InlineKeyboardButton('Назад', callback_data=prefix + f'accounts:{max(0, offset-8)}'))
                if offset + 8 < len(accounts):
                    keyboard.add(InlineKeyboardButton('Далее', callback_data=prefix + f'accounts:{offset+8}'))
                say(chat_id, 'Аккаунты' if accounts else 'Аккаунтов пока нет.', keyboard)
            elif action.startswith(('account:', 'disable:', 'check:')):
                account_id = int(action.split(':')[1])
                a = next(x for x in runtime.store.admin_accounts() if x['id'] == account_id)
                if action.startswith('disable:'):
                    runtime.store.set_disabled(account_id, not a['disabled'])
                    say(chat_id, 'Доступность для новых выдач изменена. Текущая аренда продолжается.')
                elif action.startswith('check:'):
                    account = next(x for x in runtime.store.accounts() if x['id'] == account_id)
                    items, _ = runtime.mail.Client(account).pickup()
                    say(chat_id, f'Remail ответил. Писем: {len(items)}. Коды и токены скрыты.')
                else:
                    keyboard = InlineKeyboardMarkup()
                    keyboard.add(InlineKeyboardButton('Проверить Remail', callback_data=prefix + f'check:{account_id}'))
                    keyboard.add(InlineKeyboardButton('Вернуть в пул' if a['disabled'] else 'Исключить из пула',
                                                       callback_data=prefix + f'disable:{account_id}'))
                    say(chat_id, f"<b>#{a['id']}</b> {escape(a['email'])}\nСостояние: {escape(a['state'])}"
                        f"\nЛимиты 5 ч / 7 дней: {a['five']} / {a['seven']} %"
                        f"\nЗаказ: {escape(a['order_id'] or 'нет')}\nИсключён из новых выдач: {'да' if a['disabled'] else 'нет'}", keyboard)
            elif action == 'rentals':
                rows = runtime.store.recent_rentals()
                text = '\n'.join(f"{escape(r['order_id'])}: аккаунт #{r['account_id']}, {escape(r['state'])}, "
                                 f"окончание {time.strftime('%d.%m %H:%M UTC', time.gmtime(r['expires']))}" for r in rows)
                say(chat_id, text or 'Аренд пока нет.')
        except Exception:
            say(chat_id, 'Операция не выполнена. Проверьте данные или доступ к Remail; секреты не выводятся.')

    def input_message(m):
        if not permitted(m.from_user.id, m.chat):
            return
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state or not state['state'].startswith(state_prefix):
            return
        stage = state['state'][len(state_prefix):]
        value = (m.text or '').strip()
        if stage == 'pickup':
            try:
                bot.delete_message(m.chat.id, m.id)
            except Exception:
                pass
        try:
            if stage == 'email':
                if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
                    raise ValueError('Invalid email')
                result = say(m.chat.id, 'Вставьте персональную ссылку Remail целиком. Сообщение со ссылкой будет удалено по возможности.')
                tg.set_state(m.chat.id, result.id, m.from_user.id, state_prefix + 'pickup', {'email': value})
                return
            if stage == 'pickup':
                runtime.store.add(state['data']['email'], value)
            else:
                with runtime.lock:
                    if stage == 'senders':
                        senders = [x.strip() for x in value.split(',')]
                        if not all(re.fullmatch(r'[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+', x) for x in senders):
                            raise ValueError('Invalid sender')
                        runtime.config['mailbox']['allowed_senders'] = senders
                    elif stage == 'hours':
                        hours = int(value)
                        if not 1 <= hours <= 744:
                            raise ValueError('Invalid hours')
                        runtime.config['rules'][0]['hours'] = hours
                    elif stage == 'lot':
                        if not 1 <= len(value) <= 1000:
                            raise ValueError('Invalid lot title')
                        runtime.config['rules'][0]['pattern'] = '(?i)^' + re.escape(value) + '$'
                    else:
                        return
                    runtime.save_config()
            tg.clear_state(m.chat.id, m.from_user.id)
            say(m.chat.id, 'Сохранено. Для применения настроек Claude-адаптера перезапустите Cardinal.'
                if stage == 'senders' else 'Сохранено.')
            menu(m.chat.id)
        except Exception:
            say(m.chat.id, 'Не удалось сохранить: проверьте формат, совпадение почты и ссылки или наличие дубликата. Повторите ввод или /claude_rental.')

    cardinal.add_telegram_commands(UUID, [('claude_rental', 'Аренда Claude', True)])
    tg.msg_handler(command, commands=['claude_rental'])
    tg.msg_handler(input_message, func=lambda m: bool((s := tg.get_state(m.chat.id, m.from_user.id))
                                                     and s['state'].startswith(state_prefix)), content_types=['text'])
    tg.cbq_handler(callback, lambda c: c.data.startswith(prefix) or
                   c.data.startswith(f'{CBT.PLUGIN_SETTINGS}:{UUID}:'))


def init(cardinal, *args):
    global _runtime
    if _runtime is not None:
        return
    try:
        _runtime = Runtime(cardinal, os.environ.get('CLAUDE_RENTAL_CONFIG', 'storage/claude_rental/config.json'))
        register_panel(_runtime)
    except Exception:
        log.error('Claude Rental: initialization failed; check local configuration')


def start(cardinal, *args):
    if _runtime:
        _runtime.start()


def stop(cardinal, *args):
    if _runtime:
        _runtime.shutdown()


def new_order(cardinal, event, *args):
    if _runtime:
        try:
            _runtime.enqueue(event.order)
        except Exception:
            _runtime.last_error = 'Заказ не поставлен в очередь: проверьте правило лота и состояние очереди.'
            log.error('Claude Rental: order could not be queued')


BIND_TO_DELETE = stop
BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_NEW_ORDER = [new_order]
