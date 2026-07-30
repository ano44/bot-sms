import asyncio
import os
import sys
import database as db
from telegram_checker.login_manager import login_manager

# ⚠️ أمني: لا تضع أي بيانات حقيقية (رقم هاتف، API_ID، API_HASH، أكواد تحقق) هنا مباشرة في الكود.
# القيم السابقة في هذا الملف كانت أسراراً حقيقية مكتوبة كنص صريح، وقد اعتبرناها مخترقة (Compromised)
# فور ظهورها في المستودع، ويجب تدويرها (Rotate) من حساب تيليجرام ومزود الـ API فوراً بغض النظر عن هذا الإصلاح.
# بدلاً من ذلك، مرر القيم عبر متغيرات بيئة (Environment Variables) عند التشغيل، مثال:
#   PHONE=+9677... API_ID=... API_HASH=... VERIFICATION_CODE=... python run_checker.py
PHONE = os.getenv("CHECKER_PHONE", "")
API_ID = os.getenv("CHECKER_API_ID", "")
API_HASH = os.getenv("CHECKER_API_HASH", "")

# ⚠️ إذا وصلك الكود وتريد تأكيده، مرره عبر متغير بيئة VERIFICATION_CODE، وإذا لم يصلك بعد اتركه فارغاً
VERIFICATION_CODE = os.getenv("CHECKER_VERIFICATION_CODE", "")

# ⚠️ إذا كان الحساب محمي بالتحقق بخطوتين، مرر الباسورد عبر متغير بيئة TWO_FACTOR_PASSWORD
TWO_FACTOR_PASSWORD = os.getenv("CHECKER_TWO_FACTOR_PASSWORD", "")

async def main():
    print("\n=============================================")
    print("🚀 بدء سكربت حقن الحساب الفاحص في قاعدة بيانات PostgreSQL...")
    print("=============================================\n")

    if not PHONE or not API_ID or not API_HASH:
        print("❌ يجب تمرير CHECKER_PHONE و CHECKER_API_ID و CHECKER_API_HASH كمتغيرات بيئة قبل التشغيل.")
        return

    # تأكد من تهيئة قاعدة البيانات
    try:
        db.init_db()
    except Exception as e:
        print(f"❌ خطأ في تهيئة قاعدة البيانات: {e}")
        return

    if not VERIFICATION_CODE:
        # المرحلة الأولى: إرسال الكود
        print(f"⏳ جاري الاتصال بتليجرام لإرسال كود التحقق إلى الرقم: {PHONE} ...")
        try:
            await login_manager.send_code(PHONE, API_ID, API_HASH)
            print("\n✅ تم إرسال كود التحقق بنجاح إلى حسابك في تليجرام!")
            print("📱 افتح تليجرام، خذ الكود، ثم مرره عبر متغير البيئة CHECKER_VERIFICATION_CODE وشغّل السكربت مجدداً.")
        except Exception as e:
            print(f"\n❌ حدث خطأ أثناء إرسال الكود: {e}")
        finally:
            await login_manager.cleanup()
    else:
        # المرحلة الثانية: تأكيد الكود وحفظ الجلسة
        print(f"⏳ جاري التحقق من الكود {VERIFICATION_CODE} للحساب...")
        try:
            result = await login_manager.verify_code(PHONE, VERIFICATION_CODE)
            
            if result.get("status") == "PASSWORD_REQUIRED":
                if not TWO_FACTOR_PASSWORD:
                    print("\n🔒 الحساب محمي بالتحقق بخطوتين! يرجى كتابة الباسورد في خانة TWO_FACTOR_PASSWORD وحفظ الملف مجدداً.")
                    await login_manager.cleanup()
                    return
                else:
                    print("⏳ جاري التحقق من كلمة المرور بخطوتين...")
                    result = await login_manager.verify_password(PHONE, TWO_FACTOR_PASSWORD)
            
            if result.get("status") == "SUCCESS":
                print(f"\n🎉 ✅ تم بنجاح تفعيل الحساب الفاحص وحفظه في قاعدة البيانات المشتركة!")
                print(f"👤 اسم الحساب: {result.get('name')}")
            else:
                print(f"\n❌ فشل التحقق، النتيجة: {result}")
                
        except Exception as e:
            print(f"\n❌ حدث خطأ أثناء التحقق من الكود: {e}")
        finally:
            await login_manager.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
