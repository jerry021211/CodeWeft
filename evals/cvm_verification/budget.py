"""Durable per-request reservations shared by sequential evaluation workers.

Prices are conservative peak/no-cache estimates, not provider invoices. Unknown
usage retains the full reservation; a killed worker cannot silently refund it.
"""
import json
import sqlite3
from contextlib import closing
from codeagent.runtime.execution import ExecutionStopped

PRICE_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
PRICE_DATE = '2026-10-13'
INPUT_CNY_PER_MILLION = 2
OUTPUT_CNY_PER_MILLION = 8


class Budget:
    def __init__(self, path):
        self.path = path

    def initialize(self, limit_cny, max_calls):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('CREATE TABLE limits (cny REAL, calls INTEGER); '
                             'CREATE TABLE reservations (id INTEGER PRIMARY KEY, held REAL, actual REAL, complete INTEGER DEFAULT 0);')
            db.execute('INSERT INTO limits VALUES (?, ?)', (limit_cny, max_calls))

    def reserve(self, payload):
        # Text-only fixtures. UTF-8 byte length doubled plus framing allowance is
        # deliberately conservative; image/audio requests are not in this suite.
        inputs = len(json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')) * 2 + 8192
        outputs = payload.get('max_tokens')
        if type(outputs) is not int or outputs <= 0:
            raise ValueError('Explicit output budget required')
        amount = (inputs * INPUT_CNY_PER_MILLION + outputs * OUTPUT_CNY_PER_MILLION) / 1_000_000
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            limit, calls = db.execute('SELECT cny,calls FROM limits').fetchone()
            held, count = db.execute('SELECT COALESCE(SUM(held),0),COUNT(*) FROM reservations').fetchone()
            if held + amount > limit or count >= calls:
                raise ExecutionStopped('budget_exceeded:cvm_evaluation_reservation')
            cursor = db.execute('INSERT INTO reservations (held) VALUES (?)', (amount,))
            return cursor.lastrowid

    def settle(self, key, usage):
        if hasattr(usage, 'model_dump'):
            usage = usage.model_dump()
        if not isinstance(usage, dict):
            return
        fields = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
        counts = [usage.get(k, 0) if k in fields[:2] else (usage.get(k) or 0) for k in fields]
        if any(type(n) is not int or n < 0 for n in counts) or any(k not in usage for k in fields[:2]):
            return
        actual = ((counts[0] + counts[2] + counts[3]) * INPUT_CNY_PER_MILLION + counts[1] * OUTPUT_CNY_PER_MILLION) / 1_000_000
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute('UPDATE reservations SET held=?,actual=?,complete=1 WHERE id=?', (actual, actual, key))

    def status(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            limit, cap = db.execute('SELECT cny,calls FROM limits').fetchone()
            held, actual, count, complete = db.execute('SELECT COALESCE(SUM(held),0),COALESCE(SUM(actual),0),COUNT(*),COALESCE(SUM(complete),0) FROM reservations').fetchone()
        return dict(limit_cny=limit, max_calls=cap, reserved_or_settled_cny=held,
                    reported_usage_peak_estimate_cny=actual if count == complete else None,
                    sent_calls=count, complete_usage_calls=complete, price_source=PRICE_SOURCE, price_date=PRICE_DATE,
                    note='按高峰、输入不命中缓存价格估算；未返回用量的请求保留预留额，不代表实际账单。')


class BudgetedSDK:
    def __init__(self, sdk, budget):
        self.sdk, self.budget, self.messages = sdk, budget, self

    def with_options(self, **kwargs):
        return BudgetedSDK(self.sdk.with_options(**kwargs), self.budget)

    def invoke(self, payload, call):
        key = self.budget.reserve(payload)
        response = call()
        self.budget.settle(key, getattr(response, 'usage', None))
        return response

    def create(self, **kwargs):
        return self.invoke(kwargs, lambda: self.sdk.messages.create(**kwargs))

    def post(self, path, *, body, cast_to, **kwargs):
        return self.invoke(body, lambda: self.sdk.post(path, body=body, cast_to=cast_to, **kwargs))

    def close(self):
        self.sdk.close()
