import os
from pathlib import Path

import psycopg2

conn = psycopg2.connect(os.environ["HL_APP_MIGRATION_DATABASE_URL"], connect_timeout=8)
try:
    with conn:
        with conn.cursor() as cur:
            cur.execute(Path("/docker/grant-runtime.sql").read_text(encoding="utf-8"))
finally:
    conn.close()
