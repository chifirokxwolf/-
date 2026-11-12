"""Claude Rental plugin for FunPay Cardinal 0.1.17.15.
Source edition uses sibling helper modules. dist/ contains a single-file build.
"""
import copy
import importlib.util
import json
import logging
import os
from pathlib import Path
import queue
import re
import sqlite3
import sys
import tempfile
import threading
import time
from types import ModuleType
from email.utils import parseaddr

NAME = 'Claude Rental'
VERSION = '0.3.6'
DESCRIPTION = 'Аренда Claude: аккаунты, почта Remail и управление выдачей. Требуется Claude-адаптер.'
CREDITS = 'Local development'
UUID = '52280b60-3fd9-4df7-9eb3-f1bb1cd337ef'
SETTINGS_PAGE = True
log = logging.getLogger('FPC.claude_rental')
_runtime = None


def load_rules_module():
    source = globals().get('_EMBEDDED_SOURCES', {}).get('lot_rules')
    if source is None:
        source = Path(__file__).with_name('lot_rules.py').read_text(encoding='utf-8')
    module = ModuleType('_claude_rental_rules')
    sys.modules[module.__name__] = module
    exec(compile(source, '<claude_rental/lot_rules.py>', 'exec'), module.__dict__)
    return module


rules_module = load_rules_module()
duration = rules_module.duration
DEFAULT_CONFIG = {
    'database': 'storage/claude_rental/rentals.sqlite3',
    'claude_provider_file': '',
    'enabled': False,
    'rules_schema': 1,
    'mailbox': {'allowed_senders': [], 'poll_seconds': 5, 'timeout_seconds': 120},
    'rules': rules_module.default_rules(),
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
                   for name in ('remail', 'rental', 'claude_browser_actions')}
    modules = {}
    for name in ('remail', 'rental', 'claude_browser_actions'):
        module_name = '_claude_rental_browser_actions' if name == 'claude_browser_actions' else '_claude_rental_' + name
        module = ModuleType(module_name)
        sys.modules[module_name] = module
        source = sources[name].replace('from remail import credentials, login_value',
                                      'from _claude_rental_remail import credentials, login_value')
        exec(compile(source, f'<claude_rental/{name}.py>', 'exec'), module.__dict__)
        modules[name] = module
    return modules['rental'], modules['remail']


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
            migrated = False
            if saved.get('rules') == rules_module.LEGACY_RULES:
                self.config['rules'] = rules_module.default_rules()
                migrated = True
            if saved.get('rules_schema', 0) < 1:
                for rule in self.config['rules']:
                    rule['quantity_is_duration'] = True
                self.config['rules_schema'] = 1
                migrated = True
            if migrated:
                self.save_config()
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
        self.reviews = queue.Queue(maxsize=500)
        self.review_poll_at = 0
        self.review_cursor = 0
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
            if not self.available():
                return
            if order.status.name != 'PAID':
                return
            seconds = duration(order.description, order.amount, self.config['rules'])
            if seconds is None or str(order.id) in self.queued:
                return
            task = (str(order.id), str(order.buyer_id), str(order.chat_id), seconds)
            self.store.record_purchase(*task)
            if not self.config['enabled'] or self.engine is None:
                return
            # Extend in the payment callback so another customer's mailbox wait
            # cannot let an active rental expire. Only the notice is queued.
            try:
                renewal = self.store.renew(*task)
            except self.core.Unavailable:
                renewal = None  # First delivery is still in progress; process in order.
            if renewal is not None:
                if renewal.get('duplicate'):
                    return
                task = (*task, renewal)
            self.orders.put_nowait(task)
            self.queued.add(task[0])

    def check_review(self, order):
        review = getattr(order, 'review', None)
        if (self.engine is None or order.status.name not in ('PAID', 'CLOSED') or review is None
                or type(review.stars) is not int or not 1 <= review.stars <= 5 or getattr(review, 'hidden', False)):
            return
        author = getattr(review, 'author_id', None)
        if author is not None and str(author) != str(order.buyer_id):
            return
        self.engine.apply_review(str(order.id), str(order.buyer_id))

    def queue_review(self, kind, value):
        if self.available():
            try:
                self.reviews.put_nowait((kind, value))
            except queue.Full:
                self.last_error = 'Очередь проверки отзывов заполнена; проверьте отзывы к первым покупкам.'

    def process_one(self, task, stop_event=None):
        stop_event = self.stop if stop_event is None else stop_event
        try:
            if len(task) == 5:
                if not stop_event.is_set() and self.available() and self.engine:
                    self.engine.notify_renewal(task[2], task[3], task[4])
                return
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
                        self.engine.remind()
                    except Exception:
                        self.last_error = 'Не подтверждена отправка напоминания; проверьте чаты активных аренд.'
                        log.error('Claude Rental: reminder delivery failed')
                    try:
                        if time.time() >= self.review_poll_at:
                            candidates = self.store.review_candidates()
                            if candidates:
                                for i in range(min(5, len(candidates))):
                                    self.queue_review('id', candidates[(self.review_cursor + i) % len(candidates)])
                                self.review_cursor = (self.review_cursor + 5) % len(candidates)
                            self.review_poll_at = time.time() + 60
                        if self.provider_lock.acquire(blocking=False):
                            try:
                                if not run_stop.is_set() and self.available():
                                    self.engine.tick()
                            finally:
                                self.provider_lock.release()
                    except Exception:
                        self.last_error = 'Ошибка фоновой проверки; аккаунты требуют проверки.'
                        log.error('Claude Rental: maintenance failed')
                run_stop.wait(30)

        def review_worker():
            while not run_stop.is_set():
                try:
                    kind, value = self.reviews.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    if not run_stop.is_set() and self.available():
                        order = (self.cardinal.account.get_order(value) if kind == 'id'
                                 else self.cardinal.get_order_from_object(value))
                        if order is not None:
                            self.check_review(order)
                except Exception:
                    self.last_error = 'Не удалось проверить отзыв через FunPay; бонус пока не начислен.'
                    log.error('Claude Rental: review verification failed')
                finally:
                    self.reviews.task_done()

        self.threads = [threading.Thread(target=f, daemon=True, name='claude-rental-' + label)
                        for f, label in ((delivery_worker, 'orders'), (maintenance_worker, 'maintenance'), (review_worker, 'reviews'))]
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
                f'{escape(runtime.status)}\nАккаунтов: {len(runtime.store.accounts())}'
                '\nБонус: один раз за первую покупку. Напоминание: за 15 минут.')
        if runtime.last_error:
            text += '\n' + escape(runtime.last_error)
        keyboard = InlineKeyboardMarkup(row_width=2)
        for title, action in [('Аккаунты', 'accounts:0'), ('Добавить аккаунт', 'add'),
                              ('Аренды', 'rentals'), ('Отправители Claude', 'senders'),
                              ('Лоты и сроки', 'rules'), ('Проверить название', 'preview'),
                              ('Пауза / запуск', 'toggle'), ('Обновить', 'menu'), ('Справка', 'help')]:
            keyboard.add(InlineKeyboardButton(title, callback_data=prefix + action))
        say(chat_id, text, keyboard)

    def rule_menu(chat_id):
        keyboard = InlineKeyboardMarkup(row_width=2)
        lines = ['<b>Лоты и точные сроки</b>']
        for i, rule in enumerate(runtime.config['rules']):
            label = rule.get('label', rule['pattern'])[:100]
            suffix = ' × количество' if rule.get('quantity_is_duration') else ' (фиксированный срок)'
            lines.append(f"{i+1}. {escape(label)} → {runtime.core.format_duration(rules_module.rule_seconds(rule))}{suffix}")
            keyboard.row(InlineKeyboardButton(f'Название #{i+1}', callback_data=prefix + f'edit_lot:{i}'),
                         InlineKeyboardButton(f'Срок #{i+1}', callback_data=prefix + f'edit_time:{i}'))
        keyboard.add(InlineKeyboardButton('Добавить лот', callback_data=prefix + 'add_rule'))
        keyboard.add(InlineKeyboardButton('Заменить правила на 5 стандартных', callback_data=prefix + 'reset_rules'))
        say(chat_id, '\n'.join(lines) + '\nСтандартные сроки умножаются на количество купленных единиц. '
            'Час за отзыв сюда не включается.', keyboard)

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
                ask(c, 'senders', f'Сейчас: {escape(current)}\nВведите адрес из поля «От кого» настоящего письма входа Claude. '
                    'Это не ваша почта и не ссылка Remail. Несколько адресов разделите запятой.')
            elif action in ('rules', 'lot', 'hours'):
                rule_menu(chat_id)
            elif action == 'reset_rules':
                with runtime.lock:
                    runtime.config['rules'] = rules_module.default_rules()
                    runtime.save_config()
                rule_menu(chat_id)
            elif action == 'preview':
                ask(c, 'preview', 'Вставьте название лота: покажу срок одной единицы без покупки и выдачи кода.')
            elif action == 'add_rule':
                ask(c, 'new_rule_title', 'Вставьте полное название нового лота.')
            elif action.startswith(('edit_lot:', 'edit_time:')):
                index = int(action.split(':')[1])
                data = {'index': index, 'before': copy.deepcopy(runtime.config['rules'][index])}
                if action.startswith('edit_lot:'):
                    ask(c, 'rule_title', 'Вставьте полное название лота для этого правила.', data)
                else:
                    ask(c, 'rule_minutes', 'Введите срок: 30 мин, 1 час, 2 часа и т. п. Число без единицы означает минуты.', data)
            elif action == 'help':
                say(chat_id, '<b>Как пользоваться</b>\n'
                    'Аккаунты — список, лимиты, проверка почты и исключение из выдачи.\n'
                    'Добавить аккаунт — ваша почта и персональная ссылка Remail.\n'
                    'Аренды — заказы и время окончания по Москве.\n'
                    'Отправители Claude — поле «От кого» в письме входа, не ваша почта.\n'
                    'Лоты и сроки — отдельное правило для каждого срока.\n'
                    'Проверить название — проверка распознавания без покупки.\n'
                    'Пауза / запуск — обработка новых покупок; текущие аренды продолжаются.\n'
                    'Обновить — перечитать состояние меню.\n\n'
                    'Бонус +1 час: один раз за проверенный отзыв к первой покупке до окончания первой аренды. '
                    'Продление: новый оплаченный лот во время аренды добавляет время без нового кода. '
                    'Напоминание приходит примерно за 15 минут. '
                    'Для реальных выдач и выхода из Claude нужен подключённый Claude-адаптер.')
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
                    senders = sorted({parseaddr(str(item.get('sender', '')))[1] for item in items})
                    senders = [s for s in senders if s][:10]
                    detail = '\nОтправители писем: ' + ', '.join(escape(s) for s in senders) if senders else ''
                    say(chat_id, f'Remail ответил. Писем: {len(items)}. Коды и токены скрыты.' + detail)
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
                                 f"окончание {runtime.core.end_time(r['expires'])}" for r in rows)
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
            links = [e.url for e in (m.entities or []) if e.type == 'text_link' and e.url]
            if len(links) == 1:
                value = links[0]
            elif len(links) > 1:
                value = ''  # Ask for exactly one URL; never guess which mailbox is intended.
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
            elif stage == 'preview':
                seconds = duration(value, 1, runtime.config['rules'])
                say(m.chat.id, ('Распознано: ' + runtime.core.format_duration(seconds) + '. Бонус за отзыв не включён.')
                    if seconds is not None else 'Подходящего правила нет. Откройте «Лоты и сроки».')
                tg.clear_state(m.chat.id, m.from_user.id)
                return
            elif stage == 'new_rule_title':
                if not 1 <= len(value) <= 1000:
                    raise ValueError('Invalid title')
                result = say(m.chat.id, 'Теперь введите срок: например, 30 мин или 1 час.')
                tg.set_state(m.chat.id, result.id, m.from_user.id, state_prefix + 'new_rule_minutes', {'title': value})
                return
            else:
                with runtime.lock:
                    if stage == 'senders':
                        senders = [x.strip() for x in value.split(',')]
                        if not all(re.fullmatch(r'[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+', x) for x in senders):
                            raise ValueError('Invalid sender')
                        runtime.config['mailbox']['allowed_senders'] = senders
                    elif stage in ('rule_title', 'rule_minutes'):
                        index = state['data']['index']
                        if runtime.config['rules'][index] != state['data']['before']:
                            raise ValueError('Settings changed; reopen editor')
                        rule = copy.deepcopy(runtime.config['rules'][index])
                        if stage == 'rule_title':
                            if not 1 <= len(value) <= 1000:
                                raise ValueError('Invalid title')
                            rule.update(label=value, pattern=rules_module.literal_pattern(value))
                            rule.pop('check_paid_term', None)
                        else:
                            minutes = rules_module.parse_minutes(value)
                            rule.pop('hours', None)
                            rule.pop('seconds', None)
                            rule['minutes'] = minutes
                        runtime.config['rules'][index] = rule
                    elif stage == 'new_rule_minutes':
                        if len(runtime.config['rules']) >= 20:
                            raise ValueError('Too many rules')
                        title = state['data']['title']
                        runtime.config['rules'].append({'label': title, 'pattern': rules_module.literal_pattern(title),
                                                       'minutes': rules_module.parse_minutes(value), 'quantity_is_duration': True})
                    else:
                        return
                    runtime.save_config()
            tg.clear_state(m.chat.id, m.from_user.id)
            say(m.chat.id, 'Сохранено. Для применения настроек Claude-адаптера перезапустите Cardinal.'
                if stage == 'senders' else 'Сохранено.')
            menu(m.chat.id)
        except runtime.mail.MailError as exc:
            explanations = {
                'pickup_host': 'Поддерживаются ссылки remail.best и remail.aishop6.com.',
                'pickup_email': 'Почта в ссылке не совпадает с введённой. Начните заново через /claude_rental или отправьте ссылку для этой почты.',
                'pickup_token': 'В ссылке отсутствует token или он указан несколько раз. Скопируйте персональную ссылку целиком.',
                'pickup_format': 'Отправьте одну полную ссылку HTTPS на страницу /pickup.',
            }
            say(m.chat.id, explanations.get(exc.code, 'Не удалось проверить ссылку Remail. Повторите ввод.'))
        except sqlite3.IntegrityError:
            say(m.chat.id, 'Эта почта уже добавлена. Откройте «Аккаунты» через /claude_rental.')
        except Exception:
            say(m.chat.id, 'Не удалось обработать данные: проверьте формат, единственное правило лота и срок (30 мин или 1 час). Повторите ввод или /claude_rental.')

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


def review_event(cardinal, event, *args):
    if not _runtime:
        return
    from FunPayAPI.common.enums import MessageTypes
    obj = getattr(event, 'message', None)
    if obj is not None:
        if cardinal.old_mode_enabled:
            return
        kind, mine = obj.type, obj.i_am_buyer
    else:
        if not cardinal.old_mode_enabled:
            return
        obj = getattr(event, 'chat', None)
        if obj is None:
            return
        kind = obj.last_message_type
        mine = f' {cardinal.account.username} ' in str(obj)
    if kind in (MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED) and not mine:
        _runtime.queue_review('object', obj)


def order_changed(cardinal, event, *args):
    if _runtime and event.order.status.name in ('PAID', 'CLOSED'):
        _runtime.queue_review('id', str(event.order.id))


BIND_TO_DELETE = stop
BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_NEW_MESSAGE = [review_event]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [review_event]
BIND_TO_ORDER_STATUS_CHANGED = [order_changed]
