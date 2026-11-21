"""Fictional JPY only. Allowances and purchases are facts in the kernel transaction.

An action reserves spendable funds at start. Completion binds its one debit to
the action's event; cancellation releases the hold. No owner account or real I/O.
"""
from datetime import datetime, timedelta, timezone

from .kernel import Refused

JST = timezone(timedelta(hours=9))
PERSONAL_ALLOWANCE = 40000
HOUSE_ALLOWANCE = 200000  # 公账每月拨款（日元）
# 一次性补记，只在指定世界（entry_key 防重，只记一次）
ONE_TIME_GRANTS = ()  # (world_id, 'YYYY-MM', account, yen, entry_key) for a one-off top-up of a live world


def local(at):
    return datetime.fromtimestamp(at, JST)


def month(at):
    return local(at).strftime('%Y-%m')


class Wallet:
    def __init__(self, kernel):
        self.k, self.s = kernel, kernel.s
        with self.s.tx() as db:
            db.execute("CREATE TABLE IF NOT EXISTS wallets (account TEXT PRIMARY KEY,balance INTEGER NOT NULL CHECK(balance>=0))")
            db.execute("CREATE TABLE IF NOT EXISTS wallet_entries (entry_key TEXT PRIMARY KEY,account TEXT NOT NULL REFERENCES wallets(account),delta INTEGER NOT NULL,event_id INTEGER NOT NULL REFERENCES events(event_id),at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS wallet_holds (action_id TEXT PRIMARY KEY,account TEXT NOT NULL REFERENCES wallets(account),amount INTEGER NOT NULL CHECK(amount>=0),state TEXT NOT NULL)")

    def sync(self):
        """Grant each due calendar month once, starting at activation (no historic windfall)."""
        now = self.s.clock.now()
        with self.s.tx() as db:
            active = self.s.meta('wallet_active_month')
            if active is None:
                active = month(now)
                self.s.set_meta('wallet_active_month', active)
            y, m = map(int, active.split('-'))
            accounts = [r[0] for r in db.execute('SELECT actor_id FROM actors WHERE simulated=1 ORDER BY actor_id')
                        if r[0] in self.k.roster and r[0] != 'owner'] + ['house']
            while f'{y:04d}-{m:02d}' <= month(now):
                period = f'{y:04d}-{m:02d}'
                at = datetime(y, m, 1, tzinfo=JST).timestamp()
                for account in accounts:
                    key = f'allowance:{period}:{account}'
                    if db.execute('SELECT 1 FROM wallet_entries WHERE entry_key=?', (key,)).fetchone():
                        continue
                    amount = HOUSE_ALLOWANCE if account == 'house' else PERSONAL_ALLOWANCE
                    db.execute('INSERT OR IGNORE INTO wallets VALUES(?,0)', (account,))
                    eid = self.k._emit('allowance_granted', None if account == 'house' else account, None,
                                       None, {'account': account, 'yen': amount, 'month': period},
                                       scope='system' if account == 'house' else f'self:{account}',
                                       occurred=max(at, now) if period == active else at, observers=(),
                                       source_ref='rule:monthly-allowance-v1', source_kind='rule')
                    db.execute('INSERT INTO wallet_entries VALUES(?,?,?,?,?)', (key, account, amount, eid, at))
                    db.execute('UPDATE wallets SET balance=balance+? WHERE account=?', (amount, account))
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            for world, period, account, amount, key in ONE_TIME_GRANTS:
                if self.s.synthetic or self.s.world_id != world or period < active or period > month(now) \
                        or account not in accounts:
                    continue
                if db.execute('SELECT 1 FROM wallet_entries WHERE entry_key=?', (key,)).fetchone():
                    continue
                db.execute('INSERT OR IGNORE INTO wallets VALUES(?,0)', (account,))
                eid = self.k._emit('allowance_granted', None if account == 'house' else account, None,
                                   None, {'account': account, 'yen': amount, 'month': period, 'topup': key},
                                   scope='system' if account == 'house' else f'self:{account}',
                                   occurred=now, observers=(),
                                   source_ref='owner:decision-199-topup', source_kind='rule')
                db.execute('INSERT INTO wallet_entries VALUES(?,?,?,?,?)', (key, account, amount, eid, now))
                db.execute('UPDATE wallets SET balance=balance+? WHERE account=?', (amount, account))

    def available(self, account, own=None):
        with self.s.read() as db:
            row = db.execute('SELECT balance FROM wallets WHERE account=?', (account,)).fetchone()
            held = db.execute("SELECT COALESCE(SUM(amount),0) FROM wallet_holds WHERE account=?"
                              " AND state='held' AND action_id!=?", (account, own or '')).fetchone()[0]
            return (row[0] if row else 0) - held

    def hold(self, action_id, account, amount):
        if type(amount) is not int or amount < 0:
            raise Refused('bad_price')
        with self.s.tx() as db:
            old = db.execute('SELECT * FROM wallet_holds WHERE action_id=?', (action_id,)).fetchone()
            if old:
                if (old['account'], old['amount'], old['state']) != (account, amount, 'held'):
                    raise Refused('money_hold_conflict')
                return
            if not db.execute('SELECT 1 FROM wallets WHERE account=?', (account,)).fetchone():
                raise Refused('unknown_wallet')
            if self.available(account) < amount:
                raise Refused('insufficient_funds')
            db.execute("INSERT INTO wallet_holds VALUES(?,?,?,'held')", (action_id, account, amount))

    def settle(self, action_id, event_id, at):
        with self.s.tx() as db:
            hold = db.execute("SELECT * FROM wallet_holds WHERE action_id=? AND state='held'",
                              (action_id,)).fetchone()
            if not hold:
                return
            event = db.execute('SELECT action_id FROM events WHERE event_id=?', (event_id,)).fetchone()
            action = db.execute('SELECT state FROM actions WHERE action_id=?', (action_id,)).fetchone()
            if not event or event[0] != action_id or not action or action[0] != 'succeeded':
                raise Refused('debit_without_fact')
            db.execute('INSERT INTO wallet_entries VALUES(?,?,?,?,?)',
                       (f'spend:{action_id}', hold['account'], -hold['amount'], event_id, at))
            db.execute('UPDATE wallets SET balance=balance-? WHERE account=?', (hold['amount'], hold['account']))
            db.execute("UPDATE wallet_holds SET state='spent' WHERE action_id=?", (action_id,))

    def release(self, action_id):
        with self.s.tx() as db:
            db.execute("UPDATE wallet_holds SET state='released' WHERE action_id=? AND state='held'", (action_id,))

    def transfer(self, parent, action_id, account, amount):
        with self.s.tx() as db:
            row = db.execute("SELECT amount FROM wallet_holds WHERE action_id=? AND account=? AND state='held'",
                             (parent,account)).fetchone()
            if not row or row[0] < amount:
                raise Refused('journey_funds_missing')
            db.execute('UPDATE wallet_holds SET amount=amount-? WHERE action_id=?',(amount,parent))
            self.hold(action_id,account,amount)

    def restore(self, parent, action_id):
        with self.s.tx() as db:
            row = db.execute("SELECT amount FROM wallet_holds WHERE action_id=? AND state='held'",(action_id,)).fetchone()
            if row:
                db.execute('UPDATE wallet_holds SET amount=amount+? WHERE action_id=?',(row[0],parent))
                self.release(action_id)
