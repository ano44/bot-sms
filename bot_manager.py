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
        self.running_tasks = {}  # تخزين المهام النشطة {user_id: asyncio.Task}
        self.running_apps = {}   # تخزين تطبيقات البوتات {user_id: Application}
        # قفل لكل مستخدم يمنع بدء تشغيل نسختين من نفس بوت المستخدم في نفس اللحظة
        # (كان يحدث عند تزامن استعادة البوتات بعد إعادة تشغيل السيرفر مع ضغطة المستخدم اليدوية).
        self._start_locks = defaultdict(asyncio.Lock)

    async def validate_token(self, token: str) -> bool:
        """التحقق من صحة التوكن عبر الاتصال بخوادم تيليجرام"""
        try:
            async with Bot(token) as bot:
                await bot.get_me()
            return True
        except (InvalidToken, TelegramError):
            return False

    async def start_bot(self, user_id: int, token: str) -> bool:
        """تشغيل بوت المستخدم في الخلفية دون حظر السيرفر"""
        async with self._start_locks[user_id]:
            if user_id in self.running_tasks:
                return False # البوت يعمل بالفعل

            try:
                # إنشاء التطبيق الخاص بالبوت الفرعي وتفعيل الـ Job Queue الخاص به
                app = create_user_app(token)

                # تهيئة وتحديث التطبيق بشكل صحيح مستقر
                await app.initialize()
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)

                # حفظ التطبيق في الذاكرة
                self.running_apps[user_id] = app

                # إطلاق حلقة الاستماع الصحيحة في الخلفية لضمان عدم تعليق البوت
                self.running_tasks[user_id] = asyncio.create_task(self._run_app_loop(user_id))

                await asyncio.to_thread(db.set_status, user_id, 1)
                logger.info(f"✅ تم تشغيل البوت الفرعي بنجاح للمستخدم: {user_id}")
                return True
            except Exception as e:
                logger.error(f"❌ خطأ أثناء تشغيل بوت المستخدم {user_id}: {e}")
                return False

    async def _run_app_loop(self, user_id: int):
        """إبقاء مهمة البوت الفرعي حية ومستقرة في الخلفية"""
        try:
            while user_id in self.running_apps:
                await asyncio.sleep(1) # حلقة فحص خفيفة لا تستهلك موارد الخادم
        except asyncio.CancelledError:
            logger.info(f"🔄 تم إلغاء مهمة الخلفية للبوت {user_id}")

    async def stop_bot(self, user_id: int) -> bool:
        """إيقاف البوت بشكل آمن وتحرير الموارد"""
        if user_id not in self.running_tasks:
            return False

        app = self.running_apps.get(user_id)
        task = self.running_tasks.get(user_id)

        try:
            if app:
                # إيقاف التحديثات أولاً ثم قفل التطبيق بالترتيب الصحيح
                if app.updater and app.updater.running:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
            
            if task:
                task.cancel()
        except Exception as e:
            logger.error(f"⚠️ خطأ أثناء إيقاف الموارد للبوت {user_id}: {e}")
        finally:
            # تنظيف الذاكرة في كل الأحوال
            self.running_tasks.pop(user_id, None)
            self.running_apps.pop(user_id, None)
            await asyncio.to_thread(db.set_status, user_id, 0)
            
        logger.info(f"🛑 تم إيقاف البوت وتحرير مساحته للمرسل: {user_id}")
        return True

    def get_status(self, user_id: int) -> str:
        """معرفة حالة البوت الحالية"""
        return "🟢 يعمل حالياً" if user_id in self.running_tasks else "🔴 متوقف"

    async def restore_active_bots(self):
        """استعادة كافة البوتات التي كانت تعمل قبل إعادة تشغيل السيرفر"""
        try:
            active_bots = await asyncio.to_thread(db.get_all_active_bots)
            logger.info(f"جاري استعادة {len(active_bots)} من البوتات النشطة...")
            for user_id, token in active_bots:
                # تشغيل كل بوت في مهمة مستقلة منفصلة
                asyncio.create_task(self.start_bot(user_id, token))
        except Exception as e:
            logger.error(f"Error restoring bots: {e}")

bot_manager = BotManager()
