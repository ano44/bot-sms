from database import get_connection
from datetime import datetime, timezone
import time
import asyncio

class AccountManager:
    def __init__(self):
        self._accounts_cache = None
        self._accounts_cache_ts = 0
        self._ACCOUNTS_CACHE_TTL = 30  # ثانية (مُحسَّن من 15 → 30 لتخفيف ضغط DB)
        self._cache_lock = asyncio.Lock()  # يمنع Cache Stampede عند تزامن أول قراءة بعد انتهاء الكاش
        self._rr_index = 0  # مؤشر Round-Robin لتوزيع الحمل بين الحسابات المتاحة
        self._rr_lock = asyncio.Lock()
        self._last_used_timestamps = {}
        self._COOLDOWN_PERIOD = 5  # ثوانٍ (مُحسَّن من 10 → 5 لتوزيع الحمل أسرع بين الحسابات)

    @staticmethod
    def _naive_utcnow():
        """وقت UTC الحالي بدون tzinfo (متوافق مع TIMESTAMP WITHOUT TIME ZONE في قاعدة البيانات)."""
        return datetime.now(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _is_flood_expired(flood_until, now):
        if flood_until is None:
            return True
        if flood_until.tzinfo is not None:
            flood_until = flood_until.astimezone(timezone.utc).replace(tzinfo=None)
        return flood_until <= now

    async def get_all_accounts(self):
        """ جلب جميع حسابات تيليجرام المفعلة (مع كاش 15 ثانية، محمي من Cache Stampede). """
        now = time.monotonic()
        if self._accounts_cache is not None and (now - self._accounts_cache_ts) < self._ACCOUNTS_CACHE_TTL:
            return self._accounts_cache

        # نستخدم قفلاً حتى لا تنطلق عدة استعلامات DB متزامنة لنفس التحديث (Cache Stampede)
        async with self._cache_lock:
            # إعادة الفحص بعد الحصول على القفل: ربما حدّثه طلب آخر أثناء الانتظار
            now = time.monotonic()
            if self._accounts_cache is not None and (now - self._accounts_cache_ts) < self._ACCOUNTS_CACHE_TTL:
                return self._accounts_cache

            def _fetch():
                with get_connection() as conn:
                    cur = conn.cursor()
                    try:
                        cur.execute("""
                            SELECT id, api_id, api_hash, string_session, is_active, flood_until, phone
                            FROM telegram_accounts
                            WHERE is_active = TRUE
                            ORDER BY id ASC
                        """)
                        return cur.fetchall()
                    finally:
                        cur.close()

            rows = await asyncio.to_thread(_fetch)

            accounts = []
            for row in rows:
                accounts.append({
                    "id": row[0],
                    "api_id": row[1],
                    "api_hash": row[2],
                    "session": row[3],
                    "is_active": row[4],
                    "flood_until": row[5],
                    "phone": row[6]
                })

            self._accounts_cache = accounts
            self._accounts_cache_ts = now
            return accounts

    def invalidate_accounts_cache(self):
        """ إبطال الكاش فوراً (يُستخدم عند تعطيل حساب أو تغيير حالته). """
        self._accounts_cache = None
        self._accounts_cache_ts = 0

    async def get_available_account(self):
        """
        يرجع حساباً صالحاً للاستخدام (ليس معطلاً وليس داخل FloodWait)،
        بتوزيع Round-Robin بين الحسابات المتاحة ومطابقة معدل الطلبات (Rate Limiting)
        لتوزيع الحمل بالتساوي ومنع الحظر الفردي.
        """
        accounts = await self.get_all_accounts()
        if not accounts:
            return None

        now = self._naive_utcnow()
        available = [a for a in accounts if self._is_flood_expired(a["flood_until"], now)]
        if not available:
            return None

        # تطبيق الـ Rate Limiter: تصفية الحسابات التي انتهت فترة تبريدها (Cooldown)
        now_mono = time.monotonic()
        non_cooldown = [
            a for a in available
            if now_mono - self._last_used_timestamps.get(a["id"], 0) >= self._COOLDOWN_PERIOD
        ]

        # إذا كانت جميع الحسابات في فترة التبريد، نأخذ الحسابات المتاحة بالكامل لتفادي التعليق (Fallback)
        targets = non_cooldown if non_cooldown else available

        async with self._rr_lock:
            self._rr_index = (self._rr_index + 1) % len(targets)
            chosen = targets[self._rr_index % len(targets)]

        # تحديث وقت الاستخدام للحساب المختار لتطبيق حد المعدل في المرة القادمة
        self._last_used_timestamps[chosen["id"]] = now_mono
        return chosen

    # 🚀 الدالة المضافة لحل مشكلة الـ Logs وتطابق الأسماء
    async def get_available_accounts(self):
        """ دالة إضافية بالصيغة الجمع لتفادي خطأ AttributeError في نظام الصيد """
        accounts = await self.get_all_accounts()
        if not accounts:
            return []

        now = self._naive_utcnow()
        return [a for a in accounts if self._is_flood_expired(a["flood_until"], now)]

    async def disable_account(self, account_id):
        """ تعطيل الحساب. """
        def _disable():
            with get_connection() as conn:
                cur = conn.cursor()
                try:
                    cur.execute("""
                        UPDATE telegram_accounts SET is_active = FALSE WHERE id=%s
                    """, (account_id,))
                    conn.commit()
                finally:
                    cur.close()
        await asyncio.to_thread(_disable)
        self.invalidate_accounts_cache()  # إبطال الكاش فوراً

    async def enable_account(self, account_id):
        """ إعادة تفعيل الحساب. """
        def _enable():
            with get_connection() as conn:
                cur = conn.cursor()
                try:
                    cur.execute("""
                        UPDATE telegram_accounts SET is_active = TRUE WHERE id=%s
                    """, (account_id,))
                    conn.commit()
                finally:
                    cur.close()
        await asyncio.to_thread(_enable)
        self.invalidate_accounts_cache()  # إبطال الكاش فوراً

    async def get_seconds_until_next_available(self):
        """
        في حال كانت جميع الحسابات داخل FloodWait، ترجع هذه الدالة عدد الثواني
        المتبقية لأول حساب ينتهي حظره المؤقت لكي ينام نظام الفحص بأمان.
        """
        accounts = await self.get_all_accounts()
        if not accounts:
            return 10  # قيمة افتراضية في حال عدم وجود أي حسابات مضافة

        now = self._naive_utcnow()
        flooded = [a for a in accounts if not self._is_flood_expired(a["flood_until"], now)]
        if not flooded:
            return 5  # هناك حسابات حرة ولكن ربما معطلة أو غير متوفرة لسبب آخر، ننام 5 ثوانٍ

        remaining_times = []
        for a in flooded:
            flood_until = a["flood_until"]
            if flood_until is not None:
                if flood_until.tzinfo is not None:
                    flood_until = flood_until.astimezone(timezone.utc).replace(tzinfo=None)
                diff = (flood_until - now).total_seconds()
                if diff > 0:
                    remaining_times.append(diff)

        if not remaining_times:
            return 5
        return min(remaining_times)

    async def get_all_disabled_accounts(self):
        """جلب الحسابات المعطلة لمحاولة استعادتها تلقائياً."""
        def _fetch():
            with get_connection() as conn:
                cur = conn.cursor()
                try:
                    cur.execute("""
                        SELECT id, api_id, api_hash, string_session, is_active, flood_until, phone
                        FROM telegram_accounts
                        WHERE is_active = FALSE
                        ORDER BY id ASC
                    """)
                    return cur.fetchall()
                finally:
                    cur.close()
        rows = await asyncio.to_thread(_fetch)
        accounts = []
        for row in rows:
            accounts.append({
                "id": row[0],
                "api_id": row[1],
                "api_hash": row[2],
                "session": row[3],
                "is_active": row[4],
                "flood_until": row[5],
                "phone": row[6]
            })
        return accounts

account_manager = AccountManager()
