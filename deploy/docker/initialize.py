import os
from contextlib import closing

from hl_app.auth import _auth_token, initialize
from hl_app.config import database_url
from hl_app.db import connect, migrate

url = database_url("migration")
applied = migrate(url)
print(f"migrations_applied={len(applied)}")
if os.environ.get("HL_APP_AUTH_MODE", "token") in {"cloudflare_access", "account"}:
    print("token auth initialization skipped")
else:
    token = _auth_token(os.environ)
    with closing(connect(url)) as conn, conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM auth_control WHERE singleton = true")
            initialized = cur.fetchone() is not None
    if initialized:
        print("auth already initialized")
    else:
        print(f"auth_epoch={initialize(url, token)}")
