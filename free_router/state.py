"""Persistent admission counters, cooldowns and metadata-only usage. One worker."""
import sqlite3
import time


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS counters (scope TEXT, period TEXT, count INTEGER,
          PRIMARY KEY(scope,period));
        CREATE TABLE IF NOT EXISTS cooldowns (scope TEXT PRIMARY KEY, until REAL);
        CREATE TABLE IF NOT EXISTS usage (day TEXT, client TEXT, route TEXT, result TEXT,
          count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
          PRIMARY KEY(day,client,route,result));
        ''')
        self.db.commit()

    def reserve(self, scope, rpm, daily, now=None):
        now = time.time() if now is None else now
        periods = [(f"m:{int(now // 60)}", rpm), (f"d:{int(now // 86400)}", daily)]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for period, limit in periods:
                row = self.db.execute("SELECT count FROM counters WHERE scope=? AND period=?", (scope, period)).fetchone()
                if row and row[0] >= limit:
                    return False
            for period, _ in periods:
                self.db.execute("INSERT INTO counters VALUES (?,?,1) ON CONFLICT(scope,period) DO UPDATE SET count=count+1", (scope, period))
            # Counters need only current windows; usage retains 31 UTC days.
            self.db.execute("DELETE FROM counters WHERE (period LIKE 'm:%' AND CAST(substr(period,3) AS INTEGER) < ?) OR (period LIKE 'd:%' AND CAST(substr(period,3) AS INTEGER) < ?)", (int(now // 60)-1, int(now // 86400)-1))
            self.db.execute("DELETE FROM usage WHERE CAST(day AS INTEGER) < ?", (int(now // 86400)-31,))
        return True

    def blocked(self, scope):
        row = self.db.execute("SELECT until FROM cooldowns WHERE scope=?", (scope,)).fetchone()
        return bool(row and row[0] > time.time())

    def cool(self, scope, seconds):
        with self.db:
            self.db.execute("INSERT INTO cooldowns VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET until=max(until,excluded.until)", (scope, time.time()+seconds))

    def record(self, client, route, result, usage=None):
        usage = usage or {}
        def count(name):
            n = usage.get(name, 0)
            return n if type(n) is int and 0 <= n <= 10000000 else 0
        with self.db:
            self.db.execute('''INSERT INTO usage VALUES (?,?,?,?,1,?,?)
              ON CONFLICT(day,client,route,result) DO UPDATE SET count=count+1,
              input_tokens=input_tokens+excluded.input_tokens,
              output_tokens=output_tokens+excluded.output_tokens''',
              (str(int(time.time()//86400)),client,route,result,count("prompt_tokens"),count("completion_tokens")))

    def summary(self, client):
        rows = self.db.execute("SELECT day,route,result,count,input_tokens,output_tokens FROM usage WHERE client=? ORDER BY day DESC", (client,)).fetchall()
        return [dict(zip(["utc_epoch_day","route","result","requests","input_tokens","output_tokens"], row)) for row in rows]

    def close(self):
        self.db.close()
