"""FunPay Cardinal plugin. Keep rental.py and remail.py next to this file in plugins/."""
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading

NAME = 'Claude Rental'
VERSION = '0.1.0'
DESCRIPTION = 'Exclusive rentals with persistent holds and verified cleanup'
CREDITS = 'Local development'
UUID = '52280b60-3fd9-4df7-9eb3-f1bb1cd337ef'
SETTINGS_PAGE = False
BIND_TO_DELETE = None
log = logging.getLogger('FPC.claude_rental')
_engine = None
_stop = threading.Event()
_lock = threading.Lock()


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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
    return int(rule['hours']) * 3600 * units


def init(cardinal, *args):
    global _engine, _rules
    path = Path(os.environ.get('CLAUDE_RENTAL_CONFIG', 'storage/claude_rental/config.json'))
    if not path.exists():
        log.error('Claude Rental disabled: local configuration is missing')
        return
    cfg = json.loads(path.read_text())
    _rules = cfg['rules']
    mail = load_file('remail', Path(__file__).with_name('remail.py'))
    core = load_file('_claude_rental_core', Path(__file__).with_name('rental.py'))
    if cfg.get('claude_provider_file'):
        # Claude login, usage and verified session revocation remain external prerequisites.
        claude = load_file('_claude_rental_provider', cfg['claude_provider_file']).create(cfg)
        provider = mail.Provider(claude, cfg['mailbox'])
    else:
        provider = load_file('_claude_rental_provider', cfg['provider_file']).create(cfg)
    store = core.Store(cfg.get('database', 'storage/claude_rental/rentals.sqlite3'))

    def send(chat, text):
        return bool(cardinal.send_message(chat, text, attempts=1, watermark=False))

    _engine = core.Engine(store, provider, send)
    _stop.clear()

    def worker():
        while not _stop.is_set():
            with _lock:
                _engine.tick()
            _stop.wait(30)

    threading.Thread(target=worker, name='claude-rental', daemon=True).start()


def new_order(cardinal, event, *args):
    if _engine is None:
        return
    order = event.order
    if order.status.name != 'PAID':
        return
    try:
        seconds = duration(order.description, order.amount, _rules)
        if seconds is None:
            return
        with _lock:
            _engine.deliver(str(order.id), str(order.buyer_id), str(order.chat_id), seconds)
    except Exception:
        # Third-party exception messages may contain mailbox tokens or cookies.
        log.error('Claude rental order requires operator review; no automatic reissue')


BIND_TO_POST_INIT = [init]
BIND_TO_NEW_ORDER = [new_order]
