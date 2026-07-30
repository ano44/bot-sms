import os
import asyncio
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from .account_manager import account_manager
from .login_manager import login_manager
from .checker import batch_checker

(
    API_ID,
    API_HASH,
    PHONE,
    CODE,
    PASSWORD,
    UPLOAD_FILE,
) = range(6)

user_data = {}

# 1. دالة الـ Callback الاحترافية التي تصيغ كليشة الرقم المنسقة بالأزرار الشفافة
async def send_number_report_callback(result, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """
    تستقبل النتيجة تلقائياً من الـ Checker وتقوم بصياغة الرسالة وإرسالها للمستخدم بالأزرار
    """
    phone = result["phone"]
    status_text = result.get("status_text", "🟢 الرقم بدون جلسة")
    
    # تحديد إيموجي العلم واسم الدولة (افتراضي فيجي ويمكن تخصيصه لاحقاً)
    country_name = "فيجي"
    country_flag = "🇫🇯"
    if phone.startswith("+33"):
        country_name = "فرنسا"
        country_flag = "🇫🇷"
    elif phone.startswith("+36"):
        country_name = "هنغاريا"
        country_flag = "🇭🇺"
    elif phone.startswith("+373"):
        country_name = "مولدوفا"
        country_flag = "🇲🇩"

    # كليشة التقرير المتطابقة تماماً مع النماذج المرسلة
    text_message = (
        f"🔰 تم شراء رقم جديد من **DurianRCS** 🔰\n\n"
        f"- الـرقـــم : `{phone}`\n"
        f"- الـدولـة : {country_name} {country_flag}\n"
        f"- الـحـالـة : {status_text}\n"
        f"- تكرار نزول الرقم : 1 مرة\n"
        f"- الـكـود : قيد الإنتظار ❗️"
    )
    
    # الأزرار الشفافة التفاعلية الخمسة المنسقة كما في الصور
    keyboard = [
        [
            InlineKeyboardButton("- نسبة الوصول .", callback_data=f"rate_{phone}"),
            InlineKeyboardButton("- ضعيفه 🧌 .", callback_data=f"weak_{phone}")
        ],
        [
            InlineKeyboardButton("- طلب الكود .", callback_data=f"code_{phone}"),
            InlineKeyboardButton("- فك حظر .", callback_data=f"unban_{phone}")
        ],
        [
            InlineKeyboardButton("- الغاء الرقم .", callback_data=f"cancel_{phone}")
        ]
    ]
    
    # إرسال الرسالة النهائية للمستخدم أو القناة
    await context.bot.send_message(
        chat_id=chat_id,
        text=text_message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
        [InlineKeyboardButton("📱 الحسابات", callback_data="accounts")],
        [InlineKeyboardButton("📂 رفع ملف", callback_data="upload_file")],
        [InlineKeyboardButton("▶️ بدء الفحص", callback_data="start_check")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="stats")]
    ]
    await update.message.reply_text(
        "اختر العملية المطلوبة",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def add_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("أرسل API_ID")
    return API_ID

async def receive_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    if user not in user_data:
        user_data[user] = {}
    user_data[user]["api_id"] = update.message.text.strip()
    await update.message.reply_text("الآن أرسل API_HASH")
    return API_HASH

async def receive_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    user_data[user]["api_hash"] = update.message.text.strip()
    await update.message.reply_text("أرسل رقم الهاتف")
    return PHONE

async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    phone = update.message.text.strip()
    user_data[user]["phone"] = phone
    try:
        await login_manager.send_code(
            phone=phone,
            api_id=user_data[user]["api_id"],
            api_hash=user_data[user]["api_hash"]
        )
    except Exception as e:
        await update.message.reply_text(f"❌ حدث خطأ:\n\n{e}")
        return ConversationHandler.END
        
    await update.message.reply_text("✅ تم إرسال كود تسجيل الدخول.\n\nأرسل الكود الذي وصلك.")
    return CODE

async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    code = update.message.text.strip()
    try:
        result = await login_manager.verify_code(
            phone=user_data[user]["phone"],
            code=code
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return ConversationHandler.END

    if result["status"] == "PASSWORD_REQUIRED":
        await update.message.reply_text("🔐 الحساب يحتوي على تحقق بخطوتين.\n\nأرسل كلمة المرور.")
        return PASSWORD

    await update.message.reply_text(f"✅ تم إضافة الحساب بنجاح.\n\nالاسم: {result['name']}\nالرقم: {result['phone']}")
    del user_data[user]
    return ConversationHandler.END

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    password = update.message.text.strip()
    try:
        result = await login_manager.verify_password(
            phone=user_data[user]["phone"],
            password=password
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return ConversationHandler.END

    await update.message.reply_text(f"✅ تم إضافة الحساب.\n\n{result['name']}")
    del user_data[user]
    return ConversationHandler.END

# 2. معالج تفعيل زر بدء الفحص وإرسال النتائج فورياً عبر الـ Callback الجديد
async def start_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    تُستدعى هذه الدالة عندما يضغط المستخدم على زر "▶️ بدء الفحص"
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # أمثلة للأرقام المراد فحصها (تستطيع جلبها ديناميكياً من قاعدة البيانات أو ملف الرفع لاحقاً)
    phones_to_check = ["+6797182399", "+33775813081", "+6797502745"] 

    await query.message.reply_text("🚀 جاري بدء فحص الجلسات والأرقام بالتوازي وتوليد التقارير...")

    # تثبيت سياق البوت والمعرف الخاص بالمستخدم لتشغيل الإرسال الفوري
    my_callback = lambda res: send_number_report_callback(res, context, chat_id=user_id)

    # تشغيل الفحص الجماعي المتوازي المحدث من ملف checker.py
    asyncio.create_task(batch_checker.run(phones=phones_to_check, callback=my_callback))
