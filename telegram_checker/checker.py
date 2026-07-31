import asyncio
import logging
import time
from telethon import functions, types
from telethon.errors import (
    FloodWaitError, UserPrivacyRestrictedError, PhoneNumberBannedError,
    PhoneNumberUnoccupiedError, PhoneMigrateError, PhoneCodeInvalidError,
    SessionPasswordNeededError, PhoneNumberInvalidError
)
from .telegram_client import telegram_client_manager, SessionUnauthorizedError
from .account_manager import account_manager
from .flood_manager import flood_manager
import database as db

logger = logging.getLogger(__name__)


# =====================================================================
# نظام الفحص المبسط - 3 طبقات فقط (بدون Layer 4 / Honeypot)
# مُحسَّن للعمل مع أعداد كبيرة من الحسابات الفاحصة
# =====================================================================

class FastCheckStrategy:
    """
    نظام الفحص الثلاثي السريع:
    1. ImportContacts  – فحص صامت وسريع (لا يُنبّه صاحب الرقم)
    2. ResolvePhone    – كشف الخصوصية والحظر المباشر
    3. SendCode        – التأكيد النهائي (يتم إلغاء الكود فوراً)
    """

    async def check(self, client, phone: str, account: dict) -> dict:

        # ==================== الطبقة 1: الاستيراد الصامت ====================
        logger.info(f"[L1] ImportContacts: {phone}")
        try:
            contact = types.InputPhoneContact(client_id=0, phone=phone, first_name="TC", last_name="")
            res = await asyncio.wait_for(
                client(functions.contacts.ImportContactsRequest(contacts=[contact])),
                timeout=8.0
            )

            if res.users:
                await client(functions.contacts.DeleteContactsRequest(id=[res.users[0].id]))
                logger.info(f"[L1] HAS_SESSION: {phone}")
                return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}

            if res.imported:
                await client(functions.contacts.DeleteContactsRequest(id=[res.imported[0].user_id]))
                logger.info(f"[L1] HAS_SESSION (imported): {phone}")
                return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}

        except PhoneMigrateError as e:
            try:
                await telegram_client_manager.disconnect_client(account["id"])
                client2 = await telegram_client_manager.get_client(account)
                await client2._switch_dc(e.new_dc)
                await asyncio.sleep(0.3)
                return await self.check(client2, phone, account)
            except Exception:
                pass

        except FloodWaitError as e:
            await flood_manager.set_flood(account["id"], e.seconds)
            return {"status": "FLOOD_WAIT", "seconds": e.seconds, "phone": phone,
                    "status_text": f"🚫 FloodWait {e.seconds}s"}

        except Exception as e:
            err = str(e).upper()
            logger.warning(f"[L1] Error: {e}")
            if "AUTH_KEY" in err or "BANNED" in err:
                await account_manager.disable_account(account["id"])
                return {"status": "ACCOUNT_DISABLED", "phone": phone,
                        "status_text": "❌ حساب فاحص تالف"}

        # ==================== الطبقة 2: ResolvePhone ====================
        logger.info(f"[L2] ResolvePhone: {phone}")
        try:
            resolved = await client(functions.contacts.ResolvePhoneRequest(phone=phone))
            if resolved.users:
                logger.info(f"[L2] HAS_SESSION: {phone}")
                return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}

        except UserPrivacyRestrictedError:
            # مسجّل لكن أخفى رقمه = لديه جلسة
            return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}

        except PhoneNumberUnoccupiedError:
            # غير مسجّل – ننتقل للطبقة 3 للتأكيد
            pass

        except PhoneNumberBannedError:
            return {"status": "BANNED", "phone": phone, "status_text": "📵 مـحـظـور"}

        except PhoneNumberInvalidError:
            # رقم غير صالح = غير مسجّل
            return {"status": "NO_SESSION", "phone": phone, "status_text": "🆕 غير مسجّل"}

        except FloodWaitError as e:
            await flood_manager.set_flood(account["id"], e.seconds)
            return {"status": "FLOOD_WAIT", "seconds": e.seconds, "phone": phone,
                    "status_text": f"🚫 FloodWait {e.seconds}s"}

        except Exception as e:
            err = str(e).upper()
            logger.warning(f"[L2] Error: {e}")
            if "PRIVACY" in err:
                return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}
            if "BANNED" in err:
                return {"status": "BANNED", "phone": phone, "status_text": "📵 مـحـظـور"}
            if "AUTH_KEY" in err:
                await account_manager.disable_account(account["id"])
                return {"status": "ACCOUNT_DISABLED", "phone": phone,
                        "status_text": "❌ حساب فاحص تالف"}
            if any(kw in err for kw in ["UNOCCUPIED", "NOT_FOUND", "NO USER"]):
                pass  # نكمل للطبقة 3

        # ==================== الطبقة 3: SendCode (تأكيد نهائي) ====================
        logger.info(f"[L3] SendCode: {phone}")
        try:
            if not client.is_connected():
                await client.connect()

            result = await client(functions.auth.SendCodeRequest(
                phone_number=phone,
                api_id=int(account["api_id"]),
                api_hash=account["api_hash"],
                settings=types.CodeSettings(
                    allow_flashcall=False,
                    current_number=True,
                    allow_app_hash=True
                )
            ))

            # إلغاء الكود فوراً لتجنب إزعاج صاحب الرقم
            try:
                await client(functions.auth.CancelCodeRequest(
                    phone_number=phone,
                    phone_code_hash=result.phone_code_hash
                ))
            except Exception:
                pass

            # إذا وصل للطبقة 3 بدون BANNED/NO_SESSION → الرقم مسجّل (HAS_SESSION)
            logger.info(f"[L3] HAS_SESSION (SendCode success): {phone}")
            return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}

        except PhoneNumberUnoccupiedError:
            logger.info(f"[L3] NO_SESSION: {phone}")
            return {"status": "NO_SESSION", "phone": phone, "status_text": "🆕 غير مسجّل"}

        except PhoneNumberBannedError:
            logger.info(f"[L3] BANNED: {phone}")
            return {"status": "BANNED", "phone": phone, "status_text": "📵 مـحـظـور"}

        except PhoneNumberInvalidError:
            return {"status": "NO_SESSION", "phone": phone, "status_text": "🆕 غير مسجّل"}

        except SessionPasswordNeededError:
            # 2FA مفعّل = الرقم مسجّل
            return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}

        except FloodWaitError as e:
            await flood_manager.set_flood(account["id"], e.seconds)
            return {"status": "FLOOD_WAIT", "seconds": e.seconds, "phone": phone,
                    "status_text": f"🚫 FloodWait {e.seconds}s"}

        except PhoneMigrateError as e:
            try:
                await telegram_client_manager.disconnect_client(account["id"])
                client2 = await telegram_client_manager.get_client(account)
                await client2._switch_dc(e.new_dc)
                await asyncio.sleep(0.3)
                return await self.check(client2, phone, account)
            except Exception:
                return {"status": "ERROR", "phone": phone, "status_text": "❌ فشل انتقال DC"}

        except Exception as e:
            err = str(e).upper()
            logger.error(f"[L3] Unexpected: {e}")
            if "BANNED" in err:
                return {"status": "BANNED", "phone": phone, "status_text": "📵 مـحـظـور"}
            if any(kw in err for kw in ["UNOCCUPIED", "NOT_FOUND"]):
                return {"status": "NO_SESSION", "phone": phone, "status_text": "🆕 غير مسجّل"}
            if "AUTH_KEY" in err:
                await account_manager.disable_account(account["id"])
                return {"status": "ACCOUNT_DISABLED", "phone": phone,
                        "status_text": "❌ حساب فاحص تالف"}
            return {"status": "ERROR", "phone": phone, "status_text": f"⚙️ خطأ: {e}"}


# =====================================================================
# المحرك الرئيسي
# =====================================================================

class TelegramCheckEngine:
    def __init__(self):
        self.strategy = FastCheckStrategy()

    async def check_phone(self, account: dict, phone: str) -> dict:
        # فحص الكاش أولاً (14 يوم)
        cached = await asyncio.to_thread(db.get_cached_number, phone)
        if cached:
            logger.info(f"[Cache] Hit: {phone} → {cached['status']}")
            return cached

        t_start = time.perf_counter()

        try:
            client = await telegram_client_manager.get_client(account)
        except SessionUnauthorizedError:
            await account_manager.disable_account(account["id"])
            return {"status": "ACCOUNT_DISABLED", "phone": phone,
                    "status_text": "❌ جلسة منتهية، تم تعطيل الحساب"}
        except Exception as e:
            return {"status": "ERROR", "phone": phone, "status_text": f"❌ فشل الاتصال: {e}"}

        result = await self.strategy.check(client, phone, account)

        # حفظ في الكاش فقط النتائج الحاسمة
        if result.get("status") in ("HAS_SESSION", "NO_SESSION", "BANNED"):
            await asyncio.to_thread(
                db.save_cached_number, result["phone"], result["status"], result["status_text"]
            )

        # تحديث عداد الاستخدام
        if result.get("status") not in ("FLOOD_WAIT", "ACCOUNT_DISABLED", "ERROR"):
            try:
                await flood_manager.account_used(account["id"])
            except Exception:
                pass

        elapsed = time.perf_counter() - t_start
        logger.info(f"[Check] {phone} → {result.get('status')} ({elapsed:.2f}s)")
        return result


# =====================================================================
# واجهة التوافق مع user_bot.py
# =====================================================================

class TelegramChecker:
    def __init__(self):
        self.engine = TelegramCheckEngine()

    async def _auto_recovery_loop(self):
        """استعادة الحسابات المعطلة تلقائياً كل 5 دقائق."""
        await asyncio.sleep(30)
        while True:
            try:
                disabled = await account_manager.get_all_disabled_accounts()
                for acc in disabled:
                    try:
                        client = await telegram_client_manager.get_client(acc)
                        if await client.is_user_authorized():
                            await account_manager.enable_account(acc["id"])
                            logger.info(f"[Recovery] Restored: {acc['phone']}")
                    except SessionUnauthorizedError:
                        pass
                    except Exception as e:
                        logger.warning(f"[Recovery] Failed {acc['phone']}: {e}")
            except Exception as e:
                logger.error(f"[Recovery] Loop error: {e}")
            await asyncio.sleep(300)

    async def get_available_account(self):
        if not hasattr(self, "_recovery_started"):
            self._recovery_started = True
            asyncio.create_task(self._auto_recovery_loop())
        return await account_manager.get_available_account()

    async def wait_for_account(self):
        """انتظار ذكي حتى يتوفر حساب فاحص."""
        while True:
            account = await self.get_available_account()
            if account:
                return account
            sleep_time = await account_manager.get_seconds_until_next_available()
            logger.warning(f"[Checker] كل الحسابات في FloodWait. انتظار {sleep_time:.0f}s...")
            await asyncio.sleep(min(sleep_time, 60))

    async def check_phone(self, account: dict, phone: str) -> dict:
        if not hasattr(self, "_recovery_started"):
            self._recovery_started = True
            asyncio.create_task(self._auto_recovery_loop())
        return await self.engine.check_phone(account, phone)

    async def check_numbers(self, phones: list, callback=None) -> list:
        results = []
        for phone in phones:
            account = await self.wait_for_account()
            result = await self.check_phone(account, phone)
            if result["status"] in ("FLOOD_WAIT", "ACCOUNT_DISABLED"):
                account = await self.wait_for_account()
                result = await self.check_phone(account, phone)
            results.append(result)
            if callback:
                await callback(result)
        return results


class BatchChecker:
    """فحص متعدد الحسابات المتوازي — مثالي مع عدد كبير من الحسابات الفاحصة."""

    def __init__(self, checker: TelegramChecker):
        self.checker = checker

    async def worker(self, account: dict, queue: asyncio.Queue, callback=None, active_workers=None):
        try:
            while True:
                phone = await queue.get()
                if phone is None:
                    queue.task_done()
                    break

                result = await self.checker.check_phone(account, phone)

                if result["status"] in ("FLOOD_WAIT", "FLOOD"):
                    seconds = result.get("seconds", 60)
                    await flood_manager.set_flood(account["id"], seconds)
                    await queue.put(phone)  # أعد الرقم للطابور
                    queue.task_done()
                    break

                if result["status"] in ("ACCOUNT_DISABLED",):
                    await queue.put(phone)
                    queue.task_done()
                    break

                if callback:
                    await callback(result)

                queue.task_done()
        finally:
            if active_workers:
                async with active_workers["lock"]:
                    active_workers["count"] -= 1

    async def run(self, phones: list, callback=None) -> bool:
        queue = asyncio.Queue()
        for phone in phones:
            await queue.put(phone)

        accounts = await account_manager.get_all_accounts()
        active_workers = {"count": 0, "lock": asyncio.Lock()}
        workers = []

        for account in accounts:
            if await flood_manager.is_flooded(account["id"]):
                continue
            active_workers["count"] += 1
            task = asyncio.create_task(
                self.worker(account, queue, callback, active_workers)
            )
            workers.append(task)

        if not workers:
            logger.warning("[BatchChecker] لا توجد حسابات فاحصة متاحة.")
            return False

        await queue.join()
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers, return_exceptions=True)
        return True


# سينجلتون للاستخدام في بقية الكود
telegram_checker = TelegramChecker()
batch_checker = BatchChecker(telegram_checker)
