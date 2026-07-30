from datetime import datetime, timedelta, timezone
import database
import asyncio

# ملاحظة مهمة عن التوقيت:
# عمود flood_until في قاعدة البيانات من نوع TIMESTAMP WITHOUT TIME ZONE،
# لذلك PostgreSQL/pg8000 يتعاملان معه ويُعيدانه دائماً ككائن datetime "ساذج" (naive, بلا tzinfo).
# لهذا السبب نخزّن ونقارن دائماً بتوقيت UTC "ساذج" (naive) هنا،
# لتفادي TypeError عند مقارنة aware مع naive، ولضمان أن كل القيم بنفس المرجع الزمني (UTC).

def _utcnow_naive():
    """وقت UTC الحالي بدون tzinfo، متوافق مباشرة مع TIMESTAMP WITHOUT TIME ZONE."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FloodManager:

    async def is_flooded(self, account_id):
        """
        التحقق من حالة الحظر المؤقت (FloodWait).
        """
        flood_until = await asyncio.to_thread(database.get_account_flood, account_id)
        if not flood_until:
            return False
        # حماية إضافية: لو وصلت القيمة أصلاً aware (مثلاً من مصدر آخر مستقبلاً)، نجردها من tzinfo قبل المقارنة
        if flood_until.tzinfo is not None:
            flood_until = flood_until.astimezone(timezone.utc).replace(tzinfo=None)
        return _utcnow_naive() < flood_until

    async def set_flood(self, account_id, seconds):
        """
        عند دخول الحساب FloodWait.
        """
        flood_until = _utcnow_naive() + timedelta(seconds=seconds)
        await asyncio.to_thread(database.set_account_flood, account_id, flood_until)

    async def account_used(self, account_id):
        """
        زيادة عداد الفحص.
        """
        await asyncio.to_thread(database.increase_account_checks, account_id)

    async def account_ok(self, account_id):
        """
        حالياً لا نحتاج أي شيء هنا.
        """
        return True


flood_manager = FloodManager()
