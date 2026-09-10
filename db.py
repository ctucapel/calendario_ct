import os, json, sqlite3, hashlib, hmac, secrets
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv('CALENDAR_DB', os.path.join(BASE_DIR, 'calendar.db'))
SEED_PATH = os.path.join(os.path.dirname(__file__), 'seed_data.json')

def connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=30000')
    return con

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 180000)
    return f'{salt}${digest.hex()}'

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split('$',1)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 180000).hex()
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False

def init_db():
    con=connect(); c=con.cursor()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
      unit TEXT, role TEXT NOT NULL CHECK(role IN ('Administrador','Líder','Visualizador')),
      password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS activity_groups(
      id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS activities(
      id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
      group_id INTEGER NOT NULL, description TEXT, observations TEXT,
      active INTEGER DEFAULT 1, FOREIGN KEY(group_id) REFERENCES activity_groups(id)
    );
    CREATE TABLE IF NOT EXISTS periods(
      id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, semester TEXT NOT NULL,
      start_date TEXT NOT NULL, end_date TEXT NOT NULL, UNIQUE(year,semester)
    );
    CREATE TABLE IF NOT EXISTS occurrences(
      id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id INTEGER NOT NULL, semester TEXT NOT NULL,
      start_date TEXT, end_date TEXT, period_label TEXT, FOREIGN KEY(activity_id) REFERENCES activities(id),
      UNIQUE(activity_id,semester)
    );
    CREATE TABLE IF NOT EXISTS assignments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, activity_id INTEGER NOT NULL,
      UNIQUE(user_id,activity_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(activity_id) REFERENCES activities(id)
    );
    CREATE TABLE IF NOT EXISTS dependencies(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_occurrence_id INTEGER NOT NULL, source_field TEXT NOT NULL CHECK(source_field IN ('start_date','end_date')),
      target_occurrence_id INTEGER NOT NULL, target_field TEXT NOT NULL CHECK(target_field IN ('start_date','end_date')),
      operator TEXT NOT NULL CHECK(operator IN ('>=','<=')), offset_days INTEGER NOT NULL DEFAULT 0,
      description TEXT, active INTEGER DEFAULT 1,
      FOREIGN KEY(source_occurrence_id) REFERENCES occurrences(id), FOREIGN KEY(target_occurrence_id) REFERENCES occurrences(id)
    );
    CREATE TABLE IF NOT EXISTS changes(
      id INTEGER PRIMARY KEY AUTOINCREMENT, occurrence_id INTEGER NOT NULL, requested_by INTEGER NOT NULL,
      old_start TEXT, old_end TEXT, new_start TEXT, new_end TEXT,
      status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      impact_json TEXT, admin_comment TEXT,
      FOREIGN KEY(occurrence_id) REFERENCES occurrences(id), FOREIGN KEY(requested_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS approvals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, change_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      impacted_occurrence_id INTEGER, status TEXT NOT NULL DEFAULT 'Pendiente', comment TEXT, decided_at TEXT,
      UNIQUE(change_id,user_id), FOREIGN KEY(change_id) REFERENCES changes(id), FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS notifications(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
      created_at TEXT NOT NULL, read_at TEXT, FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS occurrence_comments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, occurrence_id INTEGER NOT NULL UNIQUE, user_id INTEGER NOT NULL,
      comment TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(occurrence_id) REFERENCES occurrences(id), FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS holidays(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      year INTEGER NOT NULL,
      day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 31),
      month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
      day_type TEXT NOT NULL CHECK(day_type IN ('Feriado','Hábil')),
      holiday_type TEXT NOT NULL DEFAULT 'Variable' CHECK(holiday_type IN ('Permanente','Variable')),
      UNIQUE(year,day,month,holiday_type)
    );
    CREATE TABLE IF NOT EXISTS versions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, version_no INTEGER NOT NULL,
      status TEXT NOT NULL, change_id INTEGER, created_at TEXT NOT NULL, published_at TEXT,
      UNIQUE(year,version_no), FOREIGN KEY(change_id) REFERENCES changes(id)
    );
    CREATE TABLE IF NOT EXISTS version_occurrences(
      id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL, occurrence_id INTEGER NOT NULL,
      activity_id INTEGER NOT NULL, semester TEXT NOT NULL, start_date TEXT, end_date TEXT, period_label TEXT,
      UNIQUE(version_id,occurrence_id), FOREIGN KEY(version_id) REFERENCES versions(id)
    );
    CREATE TABLE IF NOT EXISTS audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL, entity TEXT, entity_id INTEGER,
      detail TEXT, created_at TEXT NOT NULL
    );
    ''')
    # Migraciones livianas para instalaciones ya existentes.
    cols={r['name'] for r in c.execute('PRAGMA table_info(versions)').fetchall()}
    if 'source' not in cols:
        c.execute('ALTER TABLE versions ADD COLUMN source TEXT')
    if 'description' not in cols:
        c.execute('ALTER TABLE versions ADD COLUMN description TEXT')

    dep_cols={r['name'] for r in c.execute('PRAGMA table_info(dependencies)').fetchall()}
    if 'rule_type' not in dep_cols:
        c.execute('ALTER TABLE dependencies ADD COLUMN rule_type TEXT')
        c.execute("UPDATE dependencies SET rule_type=CASE WHEN operator='<=' THEN 'antes' ELSE 'después' END WHERE rule_type IS NULL")

    # Migración de feriados V5.1: agrega año y cambia la unicidad a año/día/mes.
    holiday_cols={r['name'] for r in c.execute('PRAGMA table_info(holidays)').fetchall()}
    if holiday_cols and 'year' not in holiday_cols:
        # Los registros existentes provienen del calendario base 2027.
        c.execute('ALTER TABLE holidays RENAME TO holidays_legacy')
        c.execute("""
            CREATE TABLE holidays(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              year INTEGER NOT NULL,
              day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 31),
              month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
              day_type TEXT NOT NULL CHECK(day_type IN ('Feriado','Hábil')),
              UNIQUE(year,day,month)
            )
        """)
        c.execute('INSERT INTO holidays(id,year,day,month,day_type) SELECT id,2027,day,month,day_type FROM holidays_legacy')
        c.execute('DROP TABLE holidays_legacy')

    # Migración V5.2: agrega tipo de feriado (Permanente/Variable).
    holiday_cols={r['name'] for r in c.execute('PRAGMA table_info(holidays)').fetchall()}
    if holiday_cols and 'holiday_type' not in holiday_cols:
        c.execute("ALTER TABLE holidays ADD COLUMN holiday_type TEXT NOT NULL DEFAULT 'Variable'")
    # Índices para evitar duplicados lógicos. Para permanentes se usa year=0.
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_holiday_variable ON holidays(year,day,month) WHERE holiday_type='Variable'")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_holiday_permanent ON holidays(day,month) WHERE holiday_type='Permanente'")

    # Seed idempotente: seguro ante reinicios o inicializaciones concurrentes
    with open(SEED_PATH,encoding='utf-8') as f:
        seed=json.load(f)
    for g in seed['groups']:
        c.execute('INSERT OR IGNORE INTO activity_groups(id,name) VALUES(?,?)',(g['id'],g['name']))
    for a in seed['activities']:
        c.execute('INSERT OR IGNORE INTO activities(id,code,name,group_id,description,observations) VALUES(?,?,?,?,?,?)',
                  (a['id'],a['code'],a['name'],a['group_id'],a['description'],a.get('observations','')))
    for p in seed['periods']:
        c.execute('INSERT OR IGNORE INTO periods(year,semester,start_date,end_date) VALUES(?,?,?,?)',
                  (p['year'],p['semester'],p['start_date'],p['end_date']))
    for o in seed['occurrences']:
        c.execute('INSERT OR IGNORE INTO occurrences(activity_id,semester,start_date,end_date,period_label) VALUES(?,?,?,?,?)',
                  (o['activity_id'],o['semester'],o.get('start_date'),o.get('end_date'),o.get('period_label')))

    demo=[
      ('Administrador','Calendario','admin@demo.cl','Administración','Administrador','Admin123!'),
      ('Líder','Demo','lider@demo.cl','Unidad Académica','Líder','Lider123!'),
      ('Visualizador','Demo','visual@demo.cl','Consulta','Visualizador','Visual123!')]
    for fn,ln,email,unit,role,pwd in demo:
        c.execute('INSERT OR IGNORE INTO users(first_name,last_name,email,unit,role,password_hash) VALUES(?,?,?,?,?,?)',
                  (fn,ln,email,unit,role,hash_password(pwd)))
    leader_row=c.execute("SELECT id FROM users WHERE email='lider@demo.cl'").fetchone()
    if leader_row:
        leader_id=leader_row['id']
        for aid in (1,2,3):
            c.execute('INSERT OR IGNORE INTO assignments(user_id,activity_id) VALUES(?,?)',(leader_id,aid))
    con.commit(); con.close()

def query(sql, params=()):
    con=connect()
    try:
        rows=con.execute(sql,params).fetchall()
        con.close()
        return rows
    except sqlite3.OperationalError as e:
        con.close()
        # Recuperación automática para instalaciones existentes cuya base aún no
        # contiene una tabla agregada por una versión posterior de la aplicación.
        if 'no such table' in str(e).lower():
            init_db()
            con=connect()
            try:
                rows=con.execute(sql,params).fetchall()
                con.close()
                return rows
            except Exception:
                con.close()
                raise
        raise

def execute(sql, params=()):
    con=connect()
    try:
        cur=con.execute(sql,params); con.commit(); last=cur.lastrowid; con.close(); return last
    except sqlite3.OperationalError as e:
        con.close()
        if 'no such table' in str(e).lower():
            init_db()
            con=connect()
            try:
                cur=con.execute(sql,params); con.commit(); last=cur.lastrowid; con.close(); return last
            except Exception:
                con.close()
                raise
        raise

def audit(user_id, action, entity='', entity_id=None, detail=''):
    execute('INSERT INTO audit_log(user_id,action,entity,entity_id,detail,created_at) VALUES(?,?,?,?,?,?)',
            (user_id,action,entity,entity_id,detail,datetime.now().isoformat(timespec='seconds')))
