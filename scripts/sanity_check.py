import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from pam.config import load_dotenv
import psycopg
load_dotenv(Path(".env"))
conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
print(conn.execute("SELECT proname FROM pg_proc WHERE proname IN "
                   "('resolve_review_with_ref','resolve_review_singleton',"
                   "'apply_grouping_resolution') ORDER BY 1").fetchall())
print(conn.execute("SELECT to_regclass('manual_parent_refs')").fetchone())