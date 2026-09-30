"""Small DB-API adapter for the optional, in-memory PGlite acceptance tests."""
import json
from datetime import datetime
from pathlib import Path
import re
import subprocess


class Connection:
    def __init__(self):
        self.process = subprocess.Popen(
            ['node', str(Path(__file__).with_suffix('.cjs'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding='utf-8')
        self.in_transaction = False

    def query(self, sql, params=()):
        counter = iter(range(1, len(params) + 1))
        sql = re.sub(r'%s', lambda _: '$' + str(next(counter)), sql)
        self.process.stdin.write(json.dumps({'sql': sql, 'params': list(params)},
            default=lambda value: value.isoformat(), ensure_ascii=False) + '\n')
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            raise RuntimeError('offline PostgreSQL process exited')
        result = json.loads(response)
        if 'error' in result:
            raise RuntimeError(result['error'] + '\n' + sql)
        return result

    def cursor(self):
        return Cursor(self)

    def commit(self):
        if self.in_transaction:
            self.query('COMMIT')
        self.in_transaction = False

    def rollback(self):
        if self.in_transaction:
            self.query('ROLLBACK')
        self.in_transaction = False

    def close(self):
        # Production helpers borrow this one isolated test database.
        pass

    def shutdown(self):
        self.process.stdin.close()
        self.process.wait(timeout=20)
        self.process.stdout.close()


class Cursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        if not self.connection.in_transaction:
            self.connection.query('BEGIN')
            self.connection.in_transaction = True
        result = self.connection.query(sql, params or ())
        fields = result.get('fields', [])
        self.description = [(field['name'],) for field in fields]
        self.rows = []
        for row in result.get('rows', []):
            values = []
            for field in fields:
                value = row[field['name']]
                if value and field.get('dataTypeID') in (1114, 1184):
                    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                values.append(value)
            self.rows.append(tuple(values))
        self.rowcount = result.get('affectedRows', len(self.rows))
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        pass
