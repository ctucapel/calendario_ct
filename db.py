import os, json, sqlite3, hashlib, hmac, secrets
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv('CALENDAR_DB', os.path.join(BASE_DIR, 'calendar.db'))
SEED_PATH = os.path.join(os.path.dirname(__file__), 'seed_data.json')

def connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
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
    CREATE TABLE IF NOT EXISTS versions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, version_no INTEGER NOT NULL,
      status TEXT NOT NULL, change_id INTEGER, created_at TEXT NOT NULL, published_at TEXT,
      UNIQUE(year,version_no), FOREIGN KEY(change_id) REFERENCES changes(id)
    );
    CREATE TABLE IF NOT EXISTS audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL, entity TEXT, entity_id INTEGER,
      detail TEXT, created_at TEXT NOT NULL
    );
    ''')
    # Bootstrap seed only once
    if c.execute('SELECT COUNT(*) n FROM activity_groups').fetchone()['n']==0:
        with open(SEED_PATH,encoding='utf-8') as f: seed=json.load(f)
        for g in seed['groups']: c.execute('INSERT INTO activity_groups(id,name) VALUES(?,?)',(g['id'],g['name']))
        for a in seed['activities']:
            c.execute('INSERT INTO activities(id,code,name,group_id,description,observations) VALUES(?,?,?,?,?,?)',
                      (a['id'],a['code'],a['name'],a['group_id'],a['description'],a.get('observations','')))
        for p in seed['periods']:
            c.execute('INSERT OR IGNORE INTO periods(year,semester,start_date,end_date) VALUES(?,?,?,?)',
                      (p['year'],p['semester'],p['start_date'],p['end_date']))
        for o in seed['occurrences']:
            c.execute('INSERT INTO occurrences(activity_id,semester,start_date,end_date,period_label) VALUES(?,?,?,?,?)',
                      (o['activity_id'],o['semester'],o.get('start_date'),o.get('end_date'),o.get('period_label')))
    if c.execute('SELECT COUNT(*) n FROM users').fetchone()['n']==0:
        demo=[
          ('Administrador','Calendario','admin@demo.cl','Administración','Administrador','Admin123!'),
          ('Líder','Demo','lider@demo.cl','Unidad Académica','Líder','Lider123!'),
          ('Visualizador','Demo','visual@demo.cl','Consulta','Visualizador','Visual123!')]
        for fn,ln,email,unit,role,pwd in demo:
            c.execute('INSERT INTO users(first_name,last_name,email,unit,role,password_hash) VALUES(?,?,?,?,?,?)',
                      (fn,ln,email,unit,role,hash_password(pwd)))
        leader_id=c.execute("SELECT id FROM users WHERE email='lider@demo.cl'").fetchone()['id']
        # Demo leader can edit first three activities
        for aid in (1,2,3): c.execute('INSERT INTO assignments(user_id,activity_id) VALUES(?,?)',(leader_id,aid))
    con.commit(); con.close()

def query(sql, params=()):
    con=connect(); rows=con.execute(sql,params).fetchall(); con.close(); return rows

def execute(sql, params=()):
    con=connect(); cur=con.execute(sql,params); con.commit(); last=cur.lastrowid; con.close(); return last

def audit(user_id, action, entity='', entity_id=None, detail=''):
    execute('INSERT INTO audit_log(user_id,action,entity,entity_id,detail,created_at) VALUES(?,?,?,?,?,?)',
            (user_id,action,entity,entity_id,detail,datetime.now().isoformat(timespec='seconds')))
