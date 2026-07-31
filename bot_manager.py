import asyncio
import logging
from collections import defaultdict
from telegram import Bot
from telegram.error import InvalidToken, TelegramError
from user_bot import create_user_app
import database as db

logger = logging.getLogger(__name__)


class BotManager:
    def __init__(self):
        # {user_id: asyncio.Task}
        self.running_tasks: dict = {}
        # {user_id: Application}  
        self.running_apps: dict = {}
        # قفل مخصص لكل مستخدم يمنع تشغيل نسختين من نفس البوت في نفس اللحظة.
        # هذا يحل مشكلة ازدواج المستخدم في بوت فرعي واحد عند:
        # - الضغط المتزامن على زر "تشغيل"
        # - استعادة البوتات بعد إعادة تشغيل السيرفر + ضغط المستخدم اليدوي
        self._start_locks: dict = defaultdict(asyncio.Lock)
        # قفل للعمليات الشاملة (stop_all, restore)
        self._global_lock = asyncio.Lock()

    async def validate_token(self, token: str) -> bool:
        """التحقق من صحة التوكن عبر الاتصال بخوادم تيليجرام."""
        try:
            async with Bot(token) as bot:
                await bot.get_me()
            return True
        except (InvalidToken, TelegramError):
            return False

    async def start_bot(self, user_id: int, token: str) -> bool:
        """تشغيل بوت المستخدم في الخلفية دون حظر السيرفر.
        
        الضمانات:
        - لا يمكن تشغيل نسختين لنفس user_id في نفس الوقت (قفل مخصص).
        - إذا كان البوت يعمل بالفعل يُرجع False بدون أي عملية.
        """
        async with self._start_locks[user_id]:
            # --- فحص مزدوج داخل القفل (Double-Checked Locking) ---
            if user_id in self.running_tasks:
                # التحقق من أن المهمة ما زالت تعمل فعلاً وليست منتهية
                task = self.running_tasks[user_id]
                if not task.done():
                    logger.info(f"[BotManager] user_id={user_id}: البوت يعمل بالفعل، تخطّي.")
                    return False
                else:
                    # المهمة انتهت بشكل غير متوقع - تنظيف قبل إعادة التشغيل
                    logger.warning(f"[BotManager] user_id={user_id}: مهمة منتهية مكتشفة، إعادة تشغيل...")
                    await self._cleanup_user(user_id)

            try:
                app = create_user_app(token)
                await app.initialize()
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)

                self.running_apps[user_id] = app
                self.running_tasks[user_id] = asyncio.create_task(
                    self._run_app_loop(user_id),
                    name=f"bot_loop_{user_id}"
                )

                await asyncio.to_thread(db.set_status, user_id, 1)
                logger.info(f"✅ [BotManager] تم تشغيل البوت للمستخدم: {user_id}")
                return True

            except Exception as e:
                logger.error(f"❌ [BotManager] فشل تشغيل بوت المستخدم {user_id}: {e}")
                # تنظيف أي موارد جزئية قد تم تخصيصها
                await self._cleanup_user(user_id)
                return False

    async def _run_app_loop(self, user_id: int):
        """إبقاء مهمة البوت الفرعي حية في الخلفية.
        
        تنتهي تلقائياً إذا أُزيل التطبيق من running_apps (أي عند استدعاء stop_bot).
        """
        try:
            while user_id in self.running_apps:
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            logger.info(f"[BotManager] مهمة الخلفية للبوت {user_id} أُلغيت.")

    async def stop_bot(self, user_id: int) -> bool:
        """إيقاف البوت بشكل آمن وتحرير جميع الموارد."""
        async with self._start_locks[user_id]:
            if user_id not in self.running_tasks and user_id not in self.running_apps:
                return False

            logger.info(f"[BotManager] إيقاف بوت المستخدم: {user_id}")
            await self._cleanup_user(user_id)
            await asyncio.to_thread(db.set_status, user_id, 0)
            logger.info(f"🛑 [BotManager] تم إيقاف البوت للمستخدم: {user_id}")
            return True

    async def _cleanup_user(self, user_id: int):
        """تنظيف جميع موارد بوت مستخدم معين (داخلي).
        يُستدعى دائماً داخل _start_locks[user_id].
        """
        app = self.running_apps.pop(user_id, None)
        task = self.running_tasks.pop(user_id, None)

        # إيقاف التطبيق أولاً
        if app:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
            except Exception as e:
                logger.warning(f"[BotManager] خطأ في إيقاف تطبيق المستخدم {user_id}: {e}")

        # إلغاء مهمة الخلفية
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    def get_status(self, user_id: int) -> str:
        """معرفة حالة البوت الحالية."""
        task = self.running_tasks.get(user_id)
        if task and not task.done():
            return "🟢 يعمل حالياً"
        return "🔴 متوقف"

    def is_running(self, user_id: int) -> bool:
        """التحقق السريع من أن البوت يعمل."""
        task = self.running_tasks.get(user_id)
        return task is not None and not task.done()

    async def restore_active_bots(self):
        """استعادة كافة البوتات التي كانت تعمل قبل إعادة تشغيل السيرفر.
        
        تعمل بشكل متوازٍ لتسريع الاستعادة مع الحفاظ على منع الازدواج عبر الأقفال.
        """
        async with self._global_lock:
            try:
                active_bots = await asyncio.to_thread(db.get_all_active_bots)
                if not active_bots:
                    logger.info("[BotManager] لا توجد بوتات نشطة لاستعادتها.")
                    return

                logger.info(f"[BotManager] جاري استعادة {len(active_bots)} بوت نشط...")

                # تشغيل كل بوت في مهمة مستقلة (start_bot تمنع الازدواج عبر القفل)
                tasks = [
                    asyncio.create_task(self.start_bot(user_id, token))
                    for user_id, token in active_bots
                    if user_id not in self.running_tasks  # تخطي المشغّل بالفعل
                ]
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    success = sum(1 for r in results if r is True)
                    logger.info(f"[BotManager] اكتملت الاستعادة: {success}/{len(tasks)} بوت بنجاح.")

            except Exception as e:
                logger.error(f"[BotManager] خطأ في استعادة البوتات: {e}")

    async def get_running_count(self) -> int:
        """عدد البوتات النشطة حالياً."""
        return sum(1 for t in self.running_tasks.values() if not t.done())


bot_manager = BotManager()
