"""get_conn must request UTC when the session opens. No live database."""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import db  # noqa: E402


class GetConnTimezoneTests(unittest.TestCase):
    def test_get_conn_passes_utc_timezone_option(self):
        sentinel = object()
        with patch.object(db.psycopg2, 'connect', return_value=sentinel) as connect:
            self.assertIs(db.get_conn(), sentinel)
        connect.assert_called_once_with(
            db.DATABASE_URL,
            connect_timeout=3,
            options='-c TimeZone=UTC',
        )


if __name__ == '__main__':
    unittest.main()
