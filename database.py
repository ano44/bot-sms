import os
import time
import logging
import threading
import pg8000
from urllib.parse import urlparse

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== Thread Pool Tuning ====================
# رفع حد خيوط بايثون لتفادي تجميد البوتات تحت حمل عالي (الافتراضي = 5 × عدد الأنوية فقط)
os.environ.setdefault("PYTHON_THREADPOOL_WORKER_THREADS", "128")

DATABASE_URL = os.getenv("DATABASE_URL")

# ==================== Connection Pool ====================
# بدلاً من فتح اتصال TCP جديد في كل استدعاء (~1-2 ثانية)،
# نحتفظ بـ Pool من الاتصالات الجاهزة (~0ms).
_pool_lock = threading.Lock()
_pool: list = []  # قائمة الاتصالات الجاهزة
_POOL_SIZE = 10   # عدد الاتصالات الدائمة (مُحسَّن من 5 → 10 لتغطية حمل متعدد المستخدمين)
_MAX_TOTAL_CONNECTIONS = 35  # سقف إجمالي الاتصالات (مُحسَّن من 20 → 35)
_open_connections_sem = threading.Semaphore(_MAX_TOTAL_CONNECTIONS)

def _db_params():
    """استخراج بارامترات الاتصال من DATABASE_URL"""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set")
    parsed = urlparse(DATABASE_URL.strip())
    return dict(
        user=parsed.username,
        password=parsed.password,
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip('/'),
        ssl_context=True,
    )

def _make_conn():
    """فتح اتصال جديد مع retry"""
    params = _db_params()
    for attempt in range(5):
        try:
            conn = pg8000.connect(**params)
            return conn
        except Exception as e:
            if attempt == 4:
                logger.error(f"❌ فشلت كافة محاولات الاتصال: {e}")
                raise e
            logger.warning(f"🔄 محاولة الاتصال فشلت ({attempt + 1}/5)، إعادة المحاولة...")
            time.sleep(1)

def _is_conn_alive(conn):
    """التحقق من أن الاتصال لا يزال حياً بدون overhead كبير"""
    try:
        conn.run("SELECT 1")
        return True
    except Exception:
        return False

def get_connection():
    """الحصول على اتصال من الـ Pool أو فتح اتصال جديد إذا كان الـ Pool فارغاً.
    السقف الصارم _MAX_TOTAL_CONNECTIONS يمنع النمو غير المحدود لاتصالات DB تحت حمل مرتفع؛
    لو امتلأ السقف، الاستدعاء ينتظر حتى يُعاد اتصال ما (بدل فتح اتصال جديد بلا حدود).
    """
    while True:
        # 1) أخذ اتصال من الـ Pool بسرعة (lock فقط على pop)
        with _pool_lock:
            conn = _pool.pop() if _pool else None

        if conn is not None:
            # 2) التحقق من صلاحية الاتصال خارج الـ lock (لا نُجمّد خيوطاً أخرى)
            try:
                conn.run("SELECT 1")
                return _PooledConnection(conn)
            except Exception:
                # اتصال منتهٍ — نتخلص منه (نُحرّر مكانه في السقف) ونحاول التالي
                try:
                    conn.close()
                finally:
                    _open_connections_sem.release()
                continue

        # Pool فارغ — لا نفتح اتصالاً جديداً إلا إذا كان هناك مكان ضمن السقف الكلي
        _open_connections_sem.acquire()
        try:
            return _PooledConnection(_make_conn())
        except Exception:
            _open_connections_sem.release()
            raise

def _return_to_pool(conn):
    """إعادة الاتصال للـ Pool بعد الانتهاء منه"""
    with _pool_lock:
        if len(_pool) < _POOL_SIZE:
            _pool.append(conn)
            return
    # تجاوزنا سعة الـ Pool الدائمة: نغلق الاتصال الفعلي ونُحرّر مكانه من سقف الاتصالات الكلي
    try:
        conn.close()
    finally:
        _open_connections_sem.release()

class _PooledConnection:
    """
    Wrapper يعيد الاتصال للـ Pool بدلاً من إغلاقه عند استدعاء .close()
    يدعم context manager (with) ومتوافق 100% مع كل الكود الموجود.
    """
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def run(self, *args, **kwargs):
        return self._conn.run(*args, **kwargs)

    def close(self):
        """إعادة للـ Pool بدلاً من الإغلاق الفعلي"""
        _return_to_pool(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

def _warm_up_pool():
    """تسخين الـ Pool عند بدء التشغيل — يفتح الاتصالات مسبقاً"""
    if not DATABASE_URL:
        return
    logger.info(f"🔌 تسخين Connection Pool ({_POOL_SIZE} اتصالات)...")
    conns = []
    for i in range(_POOL_SIZE):
        try:
            conns.append(_make_conn())
        except Exception as e:
            logger.warning(f"⚠️ تعذر فتح اتصال {i+1}: {e}")
            break
    with _pool_lock:
        _pool.extend(conns)
    logger.info(f"✅ Pool جاهز: {len(_pool)} اتصالات متاحة")

# تسخين Pool تلقائياً عند استيراد الوحدة
_warm_up_pool()

def db_execute(query, params=None, commit=True, fetch=None):
    """
    Helper to execute a query safely, commit if required, and return fetched rows.
    Ensures connection is ALWAYS returned to the pool and cursor is closed.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(query, params or ())
            if commit:
                conn.commit()
            if fetch == "one":
                return cursor.fetchone()
            elif fetch == "all":
                return cursor.fetchall()
            return None
        finally:
            cursor.close()

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            # تعريف دالة column_exists قبل استخدامها
            def column_exists(table, column):
                cursor.execute(f"SELECT COUNT(*) FROM information_schema.columns WHERE table_name='{table}' AND column_name='{column}'")
                return cursor.fetchone()[0] > 0

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_bots (
                    user_id BIGINT PRIMARY KEY,
                    token TEXT UNIQUE NOT NULL,
                    is_active INTEGER DEFAULT 0,
                    expires_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP + INTERVAL '30 days',
                    is_banned INTEGER DEFAULT 0
                )
            ''')
            conn.commit()

            # إضافة عمود plan_type إذا لم يكن موجوداً (مع المسافة البادئة الصحيحة)
            if not column_exists('user_bots', 'plan_type'):
                cursor.execute("ALTER TABLE user_bots ADD COLUMN plan_type TEXT DEFAULT '1'")
                conn.commit()

            if not column_exists('user_bots', 'expires_at'):
                cursor.execute("ALTER TABLE user_bots ADD COLUMN expires_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP + INTERVAL '30 days'")
                conn.commit()
            if not column_exists('user_bots', 'is_banned'):
                cursor.execute("ALTER TABLE user_bots ADD COLUMN is_banned INTEGER DEFAULT 0")
                conn.commit()

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_hunting_channels (
                    user_id BIGINT PRIMARY KEY,
                    channel_id TEXT NOT NULL
                )
            ''')
            conn.commit()

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_hunting_status (
                    user_id BIGINT PRIMARY KEY,
                    is_hunting INTEGER DEFAULT 0
                )
            ''')
            conn.commit()

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_countries (
                    user_id BIGINT,
                    country_name TEXT,
                    PRIMARY KEY (user_id, country_name)
                )
            ''')
            conn.commit()

            # إضافة أعمدة الإعدادات إذا لم تكن موجودة
            if not column_exists('user_countries', 'number_type'):
                cursor.execute("ALTER TABLE user_countries ADD COLUMN number_type TEXT DEFAULT 'all'")
                conn.commit()
            if not column_exists('user_countries', 'session_status'):
                cursor.execute("ALTER TABLE user_countries ADD COLUMN session_status TEXT DEFAULT 'all'")
                conn.commit()
                
            # --- ترقية جدول حسابات الموقع إلى V2 (متعدد) ---
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_site_accounts_v2 (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT FALSE,
                    UNIQUE(user_id, username)
                )
            """)
            conn.commit()

            # نقل البيانات القديمة إذا كان الجدول القديم موجوداً ولم تتم ترقيته
            if not column_exists('user_site_accounts_v2', 'is_active') and column_exists('user_site_accounts', 'username') and not column_exists('user_site_accounts', 'id'):
                logger.info("Migrating old user_site_accounts to v2...")
                try:
                    cursor.execute("""
                        INSERT INTO user_site_accounts_v2 (user_id, username, api_key, is_active)
                        SELECT user_id, username, api_key, TRUE
                        FROM user_site_accounts
                        ON CONFLICT (user_id, username) DO NOTHING
                    """)
                    conn.commit()
                    cursor.execute("DROP TABLE user_site_accounts")
                    conn.commit()
                except Exception as e:
                    logger.warning(f"Migration error: {e}")
                    conn.rollback()

            # إعادة تسمية v2 إلى الاسم الأصلي
            if column_exists('user_site_accounts_v2', 'is_active'):
                cursor.execute("DROP TABLE IF EXISTS user_site_accounts")
                conn.commit()
                cursor.execute("ALTER TABLE user_site_accounts_v2 RENAME TO user_site_accounts")
                conn.commit()
                logger.info("Renamed user_site_accounts_v2 to user_site_accounts")
            else:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS user_site_accounts (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        username TEXT NOT NULL,
                        api_key TEXT NOT NULL,
                        is_active BOOLEAN DEFAULT FALSE,
                        UNIQUE(user_id, username)
                    )
                """)
                conn.commit()

            # جدول حسابات الفحص
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS telegram_accounts (
                    id SERIAL PRIMARY KEY,
                    phone TEXT UNIQUE NOT NULL,
                    api_id INTEGER NOT NULL,
                    api_hash TEXT NOT NULL,
                    string_session TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    flood_until TIMESTAMP NULL,
                    total_checks INTEGER DEFAULT 0,
                    last_used TIMESTAMP NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

            if not column_exists('telegram_accounts', 'is_active'):
                cursor.execute("ALTER TABLE telegram_accounts ADD COLUMN is_active BOOLEAN DEFAULT TRUE")
                conn.commit()
            if not column_exists('telegram_accounts', 'total_checks'):
                cursor.execute("ALTER TABLE telegram_accounts ADD COLUMN total_checks INTEGER DEFAULT 0")
                conn.commit()
            if column_exists('telegram_accounts', 'status'):
                try:
                    cursor.execute("ALTER TABLE telegram_accounts DROP COLUMN status")
                    conn.commit()
                except:
                    pass

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

            # ---------- فهارس الأداء (تُنشأ مرة واحدة فقط بشكل آمن) ----------
            # تسريع ORDER BY created_at DESC في سجل العمليات
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_activity_log_created_at
                ON activity_log (created_at DESC)
            """)
            conn.commit()
            # تسريع الفلترة بالمستخدم في سجل العمليات
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_activity_log_user_id
                ON activity_log (user_id)
            """)
            conn.commit()

            # ---------- تذاكر الدعم ----------
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    subject TEXT DEFAULT 'دعم',
                    message TEXT NOT NULL,
                    status TEXT DEFAULT 'open',
                    admin_reply TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

            # فهارس تذاكر الدعم
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_support_tickets_created_at
                ON support_tickets (created_at DESC)
            """)
            conn.commit()
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_support_tickets_user_id
                ON support_tickets (user_id)
            """)
            conn.commit()
            # فهارس الحسابات الفاحصة لتسريع استعلامات الفحص
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_telegram_accounts_is_active
                ON telegram_accounts (is_active, flood_until)
            """)
            conn.commit()
            # فهارس حسابات المستخدمين لتسريع جلبها في كل دورة صيد
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_site_accounts_user_id_active
                ON user_site_accounts (user_id, is_active)
            """)
            conn.commit()
            # فهارس دول المستخدمين
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_countries_user_id
                ON user_countries (user_id)
            """)
            conn.commit()

            # ---------- إعدادات الأسعار والمحافظ ----------
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()

            # إدخال القيم الافتراضية للإعدادات إذا لم تكن موجودة
            defaults = {
                'plan_price_1': '4',
                'plan_price_2': '6',
                'plan_price_3': '8',
                'usdt_wallet': 'TYourUSDTAddressHere',
                'trx_wallet': 'TSDqje1oWAcDY8Q5XzUDLWksWMSPqxv3PB',
                'usdt_rate': '1',   # 1 USDT = 1 USD
                'trx_rate': '0.16'   # 1 TRX = 0.16 USD (مثال، يحدد السعر)
            }
            for k, v in defaults.items():
                cursor.execute("""
                    INSERT INTO settings (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO NOTHING
                """, (k, v))
            conn.commit()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_subscriptions (
                    user_id BIGINT PRIMARY KEY,
                    plan TEXT NOT NULL,
                    payment_method TEXT NOT NULL,
                    amount_crypto TEXT NOT NULL,
                    wallet_address TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

            # ---------- جدول البروكسيات ----------
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS proxies (
                    id SERIAL PRIMARY KEY,
                    country_code TEXT NOT NULL,
                    proxy_type TEXT NOT NULL DEFAULT 'SOCKS5',
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username TEXT,
                    password TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

            # فهرس على country_code لتسريع البحث
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_proxies_country_code
                ON proxies (country_code, is_active)
            """)
            conn.commit()

            # ترقية جدول البروكسيات وإضافة الحقول الجديدة
            if not column_exists('proxies', 'provider'):
                cursor.execute("ALTER TABLE proxies ADD COLUMN provider TEXT DEFAULT 'STATIC'")
                conn.commit()
            if not column_exists('proxies', 'success_count'):
                cursor.execute("ALTER TABLE proxies ADD COLUMN success_count INTEGER DEFAULT 0")
                conn.commit()
            if not column_exists('proxies', 'failure_count'):
                cursor.execute("ALTER TABLE proxies ADD COLUMN failure_count INTEGER DEFAULT 0")
                conn.commit()
            if not column_exists('proxies', 'avg_latency'):
                cursor.execute("ALTER TABLE proxies ADD COLUMN avg_latency REAL DEFAULT 0.0")
                conn.commit()
            if not column_exists('proxies', 'flood_count'):
                cursor.execute("ALTER TABLE proxies ADD COLUMN flood_count INTEGER DEFAULT 0")
                conn.commit()
            if not column_exists('proxies', 'last_used'):
                cursor.execute("ALTER TABLE proxies ADD COLUMN last_used TIMESTAMP NULL")
                conn.commit()
            if not column_exists('proxies', 'rotation_url'):
                cursor.execute("ALTER TABLE proxies ADD COLUMN rotation_url TEXT NULL")
                conn.commit()

            # ---------- التخزين المؤقت للأرقام المفحوصة ----------
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS checked_numbers_cache (
                    phone VARCHAR(50) PRIMARY KEY,
                    status VARCHAR(50) NOT NULL,
                    status_text VARCHAR(255) NOT NULL,
                    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            cursor.execute("DELETE FROM checked_numbers_cache WHERE checked_at < NOW() - INTERVAL '14 days'")
            conn.commit()

        finally:
            cursor.close()

# --- دوال حسابات DurianRCS (متعددة) ---
def save_site_account_v2(user_id, username, api_key):
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            # حد الحسابات بناءً على الخطة
            plan = get_user_plan(user_id)
            max_accounts = int(plan)
            cursor.execute("SELECT COUNT(*) FROM user_site_accounts WHERE user_id = %s", (user_id,))
            count = cursor.fetchone()[0]
            if count >= max_accounts:
                raise Exception("MAX_ACCOUNTS_REACHED")
            cursor.execute("""
                INSERT INTO user_site_accounts (user_id, username, api_key, is_active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (user_id, username) DO UPDATE SET api_key = EXCLUDED.api_key, is_active = TRUE
            """, (user_id, username, api_key))
            conn.commit()
        finally:
            cursor.close()

def get_all_site_accounts(user_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, username, api_key, is_active FROM user_site_accounts WHERE user_id = %s ORDER BY id", (user_id,))
            rows = cursor.fetchall()
            return rows
        finally:
            cursor.close()

def toggle_site_account(user_id, account_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT is_active FROM user_site_accounts WHERE id = %s AND user_id = %s", (account_id, user_id))
            row = cursor.fetchone()
            if not row:
                return
            current_status = row[0]
            if not current_status:  # سيُفعّل الآن
                plan = get_user_plan(user_id)
                max_active = int(plan)
                cursor.execute("SELECT COUNT(*) FROM user_site_accounts WHERE user_id = %s AND is_active = TRUE", (user_id,))
                active_count = cursor.fetchone()[0]
                if active_count >= max_active:
                    raise Exception("MAX_ACTIVE_REACHED")
            cursor.execute("""
                UPDATE user_site_accounts
                SET is_active = NOT is_active
                WHERE id = %s AND user_id = %s
            """, (account_id, user_id))
            conn.commit()
        finally:
            cursor.close()

def delete_site_account(user_id, account_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM user_site_accounts WHERE id = %s AND user_id = %s", (account_id, user_id))
            cursor.execute("SELECT COUNT(*) FROM user_site_accounts WHERE user_id = %s AND is_active = TRUE", (user_id,))
            if cursor.fetchone()[0] == 0:
                cursor.execute("UPDATE user_site_accounts SET is_active = TRUE WHERE id = (SELECT id FROM user_site_accounts WHERE user_id = %s LIMIT 1)", (user_id,))
            conn.commit()
        finally:
            cursor.close()

def get_site_account(user_id):
    """الحساب النشط فقط"""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT username, api_key FROM user_site_accounts WHERE user_id = %s AND is_active = TRUE LIMIT 1", (user_id,))
            row = cursor.fetchone()
            return row
        finally:
            cursor.close()

def get_active_site_accounts(user_id):
    """استرجاع جميع حسابات الموقع النشطة للمستخدم"""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT username, api_key FROM user_site_accounts WHERE user_id = %s AND is_active = TRUE",
                (user_id,)
            )
            rows = cursor.fetchall()
            return rows  # list of (username, api_key)
        finally:
            cursor.close()

# --- باقي الدوال (موجودة مسبقاً) ---
def save_bot(user_id, token):
    db_execute('''
        INSERT INTO user_bots (user_id, token, is_active, expires_at, is_banned) 
        VALUES (%s, %s, 0, CURRENT_TIMESTAMP + INTERVAL '30 days', 0)
        ON CONFLICT (user_id) 
        DO UPDATE SET token = EXCLUDED.token;
    ''', (user_id, token))

def add_days_to_user(user_id, days, plan_type=None):
    row = db_execute('SELECT token FROM user_bots WHERE user_id = %s', (user_id,), commit=False, fetch='one')
    if row:
        if plan_type:
            db_execute('''
                UPDATE user_bots
                SET expires_at = GREATEST(expires_at, CURRENT_TIMESTAMP) + CAST(%s AS INTERVAL),
                    plan_type = %s
                WHERE user_id = %s
            ''', (f"{days} days", plan_type, user_id))
        else:
            db_execute('''
                UPDATE user_bots
                SET expires_at = GREATEST(expires_at, CURRENT_TIMESTAMP) + CAST(%s AS INTERVAL)
                WHERE user_id = %s
            ''', (f"{days} days", user_id))
    else:
        temp_token = f'pending_{user_id}'
        if plan_type:
            db_execute('''
                INSERT INTO user_bots (user_id, token, is_active, expires_at, is_banned, plan_type)
                VALUES (%s, %s, 0, CURRENT_TIMESTAMP + CAST(%s AS INTERVAL), 0, %s)
            ''', (user_id, temp_token, f"{days} days", plan_type))
        else:
            db_execute('''
                INSERT INTO user_bots (user_id, token, is_active, expires_at, is_banned)
                VALUES (%s, %s, 0, CURRENT_TIMESTAMP + CAST(%s AS INTERVAL), 0)
            ''', (user_id, temp_token, f"{days} days"))

def get_user_plan(user_id):
    """جلب نوع خطة المستخدم (1, 2, 3)"""
    try:
        row = db_execute("SELECT plan_type FROM user_bots WHERE user_id = %s", (user_id,), commit=False, fetch='one')
        return row[0] if row else '1'
    except Exception:
        return '1'

def ban_user(user_id, status):
    db_execute('UPDATE user_bots SET is_banned = %s WHERE user_id = %s', (status, user_id))

def set_status(user_id, is_active):
    db_execute('UPDATE user_bots SET is_active = %s WHERE user_id = %s', (is_active, user_id))

def get_bot(user_id):
    try:
        return db_execute('SELECT token, is_active, expires_at, is_banned FROM user_bots WHERE user_id = %s', (user_id,), commit=False, fetch='one')
    except Exception:
        return None

def get_all_active_bots():
    try:
        return db_execute('SELECT user_id, token FROM user_bots WHERE is_active = 1 AND is_banned = 0', commit=False, fetch='all')
    except Exception:
        return []

def get_stats():
    try:
        total = db_execute('SELECT COUNT(*) FROM user_bots', commit=False, fetch='one')
        active = db_execute('SELECT COUNT(*) FROM user_bots WHERE is_active = 1', commit=False, fetch='one')
        return (total[0] if total else 0), (active[0] if active else 0)
    except Exception:
        return 0, 0

def save_hunting_channel(user_id, channel_id):
    db_execute('''
        INSERT INTO user_hunting_channels (user_id, channel_id)
        VALUES (%s, %s)
        ON CONFLICT (user_id)
        DO UPDATE SET channel_id = EXCLUDED.channel_id;
    ''', (user_id, str(channel_id)))

def get_hunting_channel(user_id):
    # ملاحظة: لا نُبلع أي استثناء هنا عمداً. لو فشل الاتصال بقاعدة البيانات فعلاً،
    # يجب أن يصل الخطأ للمتصل (check_and_hunt_numbers) ليُعامله كـ "عطل مؤقت"
    # لا كـ "لا توجد قناة"، وإلا يُلغى Job الصيد نهائياً بالخطأ.
    row = db_execute('SELECT channel_id FROM user_hunting_channels WHERE user_id = %s', (user_id,), commit=False, fetch='one')
    return row[0] if row else None

def set_hunting_status(user_id, is_hunting):
    status_val = 1 if is_hunting else 0
    db_execute('''
        INSERT INTO user_hunting_status (user_id, is_hunting)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET is_hunting = EXCLUDED.is_hunting
    ''', (user_id, status_val))

def get_user_countries(user_id):
    # نفس الملاحظة أعلاه: لا نُخفي استثناءات DB الحقيقية بإعادة قائمة فارغة،
    # حتى لا يُخطئ check_and_hunt_numbers فيظن أن المستخدم لم يحدد دولاً فيلغي مهمة الصيد نهائياً.
    rows = db_execute('SELECT country_name FROM user_countries WHERE user_id = %s', (user_id,), commit=False, fetch='all')
    return [row[0] for row in rows] if rows else []

def add_user_country(user_id, country_name):
    db_execute('''
        INSERT INTO user_countries (user_id, country_name)
        VALUES (%s, %s)
        ON CONFLICT (user_id, country_name) DO NOTHING
    ''', (user_id, country_name))

def delete_user_country(user_id, country_name):
    db_execute("DELETE FROM user_countries WHERE user_id = %s AND country_name = %s", (user_id, country_name))

# ---------- نظام الاشتراكات المعلقة ----------
def add_pending_subscription(user_id, plan, payment_method, amount_crypto, wallet_address):
    db_execute("""
        INSERT INTO pending_subscriptions (user_id, plan, payment_method, amount_crypto, wallet_address)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            plan = EXCLUDED.plan,
            payment_method = EXCLUDED.payment_method,
            amount_crypto = EXCLUDED.amount_crypto,
            wallet_address = EXCLUDED.wallet_address,
            created_at = CURRENT_TIMESTAMP
    """, (user_id, plan, payment_method, amount_crypto, wallet_address))

def get_pending_subscription(user_id):
    return db_execute("SELECT plan, payment_method, amount_crypto, wallet_address, created_at FROM pending_subscriptions WHERE user_id = %s", (user_id,), commit=False, fetch='one')

def claim_pending_subscription(user_id):
    """
    يقرأ الاشتراك المعلّق ويحذفه في نفس العملية الذرية (DELETE ... RETURNING)،
    بدل قراءة ثم حذف كخطوتين منفصلتين. هذا يمنع تنفيذ نفس طلب الدفع مرتين
    لو ضغط الأدمن زر التأكيد ضغطتين متتاليتين بسرعة (Race Condition):
    الضغطة الثانية ستجد لا شيء لتحذفه وتُرجع None بدل تكرار تفعيل الاشتراك.
    """
    return db_execute(
        "DELETE FROM pending_subscriptions WHERE user_id = %s "
        "RETURNING plan, payment_method, amount_crypto, wallet_address, created_at",
        (user_id,), commit=True, fetch='one'
    )

def delete_pending_subscription(user_id):
    db_execute("DELETE FROM pending_subscriptions WHERE user_id = %s", (user_id,))

def get_all_pending_subscriptions():
    return db_execute("SELECT user_id, plan, payment_method, amount_crypto, wallet_address, created_at FROM pending_subscriptions ORDER BY created_at DESC", commit=False, fetch='all')

def get_all_checkers():
    return db_execute("SELECT id, phone, is_active, total_checks FROM telegram_accounts ORDER BY id", commit=False, fetch='all')

def delete_checker(account_id):
    db_execute("DELETE FROM telegram_accounts WHERE id = %s", (account_id,))

def toggle_checker(account_id):
    db_execute("UPDATE telegram_accounts SET is_active = NOT is_active WHERE id = %s", (account_id,))

def get_account_flood(account_id):
    try:
        row = db_execute("SELECT flood_until FROM telegram_accounts WHERE id=%s", (account_id,), commit=False, fetch='one')
        return row[0] if row else None
    except Exception:
        return None

def save_telegram_account(phone, api_id, api_hash, string_session):
    db_execute("""
        INSERT INTO telegram_accounts (phone, api_id, api_hash, string_session)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (phone) DO UPDATE SET
            api_id=EXCLUDED.api_id,
            api_hash=EXCLUDED.api_hash,
            string_session=EXCLUDED.string_session,
            is_active = TRUE
    """, (phone, api_id, api_hash, string_session))

def get_telegram_accounts():
    return db_execute("""
        SELECT id, phone, api_id, api_hash, string_session, is_active,
               flood_until, total_checks, last_used
        FROM telegram_accounts
        ORDER BY id
    """, commit=False, fetch='all')

def delete_telegram_account(account_id):
    db_execute("DELETE FROM telegram_accounts WHERE id=%s", (account_id,))

def set_account_flood(account_id, flood_until):
    db_execute("UPDATE telegram_accounts SET flood_until=%s WHERE id=%s", (flood_until, account_id))

def increase_account_checks(account_id):
    db_execute("""
        UPDATE telegram_accounts SET
            total_checks = total_checks + 1,
            last_used = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (account_id,))

def get_best_telegram_account():
    return db_execute("""
        SELECT id, phone, api_id, api_hash, string_session
        FROM telegram_accounts
        WHERE is_active = TRUE
          AND (flood_until IS NULL OR flood_until < CURRENT_TIMESTAMP)
        ORDER BY total_checks ASC, last_used ASC NULLS FIRST
        LIMIT 1
    """, commit=False, fetch='one')

# ---------- سجل العمليات ----------
def log_activity(user_id, action, details=""):
    db_execute("INSERT INTO activity_log (user_id, action, details) VALUES (%s, %s, %s)", (user_id, action, details))

def get_recent_activities(limit=50):
    return db_execute("SELECT id, user_id, action, details, created_at FROM activity_log ORDER BY created_at DESC LIMIT %s", (limit,), commit=False, fetch='all')

# ---------- تذاكر الدعم ----------
def create_ticket(user_id, subject, message):
    db_execute("INSERT INTO support_tickets (user_id, subject, message) VALUES (%s, %s, %s)", (user_id, subject, message))

def get_open_tickets():
    return db_execute("SELECT id, user_id, subject, message, status, admin_reply, created_at FROM support_tickets WHERE status='open' ORDER BY created_at DESC", commit=False, fetch='all')

def get_all_tickets():
    return db_execute("SELECT id, user_id, subject, message, status, admin_reply, created_at FROM support_tickets ORDER BY created_at DESC", commit=False, fetch='all')

def reply_ticket(ticket_id, reply_text):
    db_execute("UPDATE support_tickets SET admin_reply = %s, status = 'closed', updated_at = CURRENT_TIMESTAMP WHERE id = %s", (reply_text, ticket_id))

def close_ticket(ticket_id):
    db_execute("UPDATE support_tickets SET status = 'closed', updated_at = CURRENT_TIMESTAMP WHERE id = %s", (ticket_id,))

# ---------- الإعدادات ----------
def get_setting(key, default=None):
    row = db_execute("SELECT value FROM settings WHERE key = %s", (key,), commit=False, fetch='one')
    if row:
        return row[0]
    return default

def set_setting(key, value):
    db_execute("""
        INSERT INTO settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """, (key, str(value)))

def get_all_settings():
    return db_execute("SELECT key, value FROM settings ORDER BY key", commit=False, fetch='all')

def update_country_settings(user_id, country_code, number_type=None, session_status=None):
    if number_type is not None:
        db_execute("UPDATE user_countries SET number_type = %s WHERE user_id = %s AND country_name = %s", (number_type, user_id, country_code))
    if session_status is not None:
        db_execute("UPDATE user_countries SET session_status = %s WHERE user_id = %s AND country_name = %s", (session_status, user_id, country_code))

def get_country_settings(user_id, country_code):
    row = db_execute("SELECT number_type, session_status FROM user_countries WHERE user_id = %s AND country_name = %s", (user_id, country_code), commit=False, fetch='one')
    if row:
        return {"number_type": row[0] or "all", "session_status": row[1] or "all"}
    return {"number_type": "all", "session_status": "all"}


# ==================== دوال البروكسيات ====================

def add_proxy(country_code, host, port, username=None, password=None, proxy_type='SOCKS5', provider='STATIC', rotation_url=None):
    """إضافة بروكسي جديد لدولة معينة."""
    db_execute("""
        INSERT INTO proxies (country_code, proxy_type, host, port, username, password, is_active, provider, rotation_url)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s)
    """, (country_code.upper(), proxy_type.upper(), host, int(port), username, password, provider.upper(), rotation_url))

def get_proxy_for_country(country_code):
    """جلب بروكسي نشط لدولة معينة (مُرتّب حسب الأعلى تقييماً لتفادي التالف)."""
    row = db_execute("""
        SELECT id, proxy_type, host, port, username, password, provider, rotation_url
        FROM proxies
        WHERE country_code = %s AND is_active = TRUE
        ORDER BY (success_count / COALESCE(NULLIF(success_count + failure_count, 0), 1.0)) DESC, success_count DESC
        LIMIT 1
    """, (country_code.upper(),), commit=False, fetch='one')
    if row:
        return {
            "id": row[0],
            "proxy_type": row[1],
            "host": row[2],
            "port": row[3],
            "username": row[4],
            "password": row[5],
            "provider": row[6],
            "rotation_url": row[7],
            "country_code": country_code.upper()
        }
    return None

def get_all_proxies():
    """جلب جميع البروكسيات مع الإحصائيات."""
    rows = db_execute("""
        SELECT id, country_code, proxy_type, host, port, username, password, is_active, created_at, provider, success_count, failure_count, avg_latency
        FROM proxies
        ORDER BY country_code, id
    """, commit=False, fetch='all')
    if not rows:
        return []
    return [
        {
            "id": r[0],
            "country_code": r[1],
            "proxy_type": r[2],
            "host": r[3],
            "port": r[4],
            "username": r[5],
            "password": r[6],
            "is_active": r[7],
            "created_at": r[8],
            "provider": r[9],
            "success_count": r[10],
            "failure_count": r[11],
            "avg_latency": r[12]
        }
        for r in rows
    ]

def update_proxy_stats(proxy_id, is_success, latency=0.0, is_flood=False):
    """تحديث إحصائيات البروكسي."""
    if is_success:
        db_execute("""
            UPDATE proxies 
            SET success_count = success_count + 1, 
                avg_latency = (avg_latency * 0.9) + (%s * 0.1),
                last_used = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (float(latency), proxy_id))
    else:
        flood_increment = 1 if is_flood else 0
        db_execute("""
            UPDATE proxies 
            SET failure_count = failure_count + 1,
                flood_count = flood_count + %s,
                last_used = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (flood_increment, proxy_id))

def delete_proxy(proxy_id):
    """حذف بروكسي بالـ ID."""
    db_execute("DELETE FROM proxies WHERE id = %s", (proxy_id,))

def toggle_proxy(proxy_id, is_active):
    """تفعيل أو تعطيل بروكسي."""
    db_execute("UPDATE proxies SET is_active = %s WHERE id = %s", (is_active, proxy_id))


# ---------- التخزين المؤقت للأرقام المفحوصة (Cache) ----------

def get_cached_number(phone):
    """البحث عن رقم في التخزين المؤقت وإرجاع نتيجته إذا لم يمر عليها 14 يوم"""
    row = db_execute("""
        SELECT status, status_text 
        FROM checked_numbers_cache 
        WHERE phone = %s AND checked_at >= NOW() - INTERVAL '14 days'
    """, (phone,), commit=False, fetch='one')
    if row:
        return {"status": row[0], "phone": phone, "status_text": row[1]}
    return None

def save_cached_number(phone, status, status_text):
    """حفظ أو تحديث نتيجة فحص رقم في التخزين المؤقت"""
    db_execute("""
        INSERT INTO checked_numbers_cache (phone, status, status_text, checked_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (phone) DO UPDATE 
        SET status = EXCLUDED.status, 
            status_text = EXCLUDED.status_text, 
            checked_at = EXCLUDED.checked_at
    """, (phone, status, status_text))
