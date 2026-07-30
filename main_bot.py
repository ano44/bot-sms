import os
import asyncio
import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from telegram.request import HTTPXRequest
import telegram.error
import database as db
from bot_manager import bot_manager
from telegram_checker.login_manager import login_manager
from proxy_infrastructure import check_all_proxies_health

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

MAIN_TOKEN = os.getenv("MAIN_BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except Exception:
    ADMIN_ID = 0

PHONE, CODE, PASSWORD = range(3)

async def show_welcome(update: Update, user_name: str):
    text = (
        f"😁👋 ❛ ≽ السلام عليكم ورحمة الله وبركاته ≼\n\n"
        f"👤 ❛ ≽ حياك الله يا {user_name} 🎊، أهلاً وسهلاً ومرحباً بك. ≼\n\n"
        f"🤖 ❛ ≽ البوت المميز والحصري في تقديم خدمة صيد الأرقام مع فك الحظر التلقائي عن الأرقام. ≼\n\n"
        f"📮 ❛ ≽ كل ما عليك فقط أن تشترك في البوت لتبدأ رحلت صيد الأرقام العالمية والدولية 📱 ≼\n\n"
        f"ماذا تنتظر...!؟\n≽ ≽ ≽ اضغط هنا وابدأ 🔻"
    )
    keyboard = [[InlineKeyboardButton("اشترك الان", callback_data="subscribe")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup)

def get_correct_table_name():
    return "user_bots"

async def set_honeypot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_ID == 0 or user_id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("الرجاء إرسال الرقم. مثال:\n/set_honeypot +123456789")
        return
    phone = context.args[0]
    await asyncio.to_thread(db.set_setting, "honeypot_number", phone)
    await update.message.reply_text(f"✅ تم حفظ رقم الفخ بنجاح: {phone}\n\nسيقوم البوت الآن بفحص هذا الرقم تلقائياً لاختبار الحسابات التي تدعي عدم وجود أرقام الصيد للتأكد من سلامتها من حظر الظل.")

async def set_checker_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_ID == 0 or user_id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("الرجاء إرسال معرف البوت. مثال:\n/set_checker_bot SessionCheckerReBoT\n\nلإلغاء التفعيل أرسل: /set_checker_bot off")
        return
    bot_username = context.args[0].replace("@", "")
    if bot_username.lower() == "off":
        await asyncio.to_thread(db.set_setting, "checker_bot_username", "")
        await update.message.reply_text("❌ تم تعطيل الربط مع بوت الفحص الخارجي.")
    else:
        await asyncio.to_thread(db.set_setting, "checker_bot_username", bot_username)
        await update.message.reply_text(f"✅ تم تفعيل الربط مع البوت: @{bot_username}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    db_data = await asyncio.to_thread(db.get_bot, user_id)
    if db_data and len(db_data) >= 4:
        if db_data[3] == 1:
            await update.message.reply_text("❌ عذراً، تم إيقاف حسابك وحظرك من استخدام المنصة من قبل الإدارة.")
            return
        # تحقق من الاشتراك
        expires_at = db_data[2]
        if expires_at:
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", ""))
            if expires_at.replace(tzinfo=None) > datetime.now(timezone.utc).replace(tzinfo=None):
                await show_dashboard(update, user_id, user_name)
                return
    # لا يوجد اشتراك ساري
    await show_welcome(update, user_name)

async def handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    text = update.message.text.strip()

    if context.user_data.get("waiting_for_token"):
        context.user_data.pop("waiting_for_token", None)
        # التحقق من صحة التوكن
        is_valid = await bot_manager.validate_token(text)
        if not is_valid:
            await update.message.reply_text("❌ التوكن غير صالح! تأكد من الحصول عليه بشكل صحيح من @BotFather.")
            return
        try:
            await asyncio.to_thread(db.save_bot, user_id, text)
            await update.message.reply_text(
                f"✅ تم تحديث توكن البوت بنجاح\n\n"
                f"🔑 التوكن: <code>{text}</code>",
                parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ في قاعدة البيانات: {e}")
        return

    if ADMIN_ID != 0 and user_id == ADMIN_ID and context.user_data.get("admin_action") == "ticket_reply":
        ticket_id = context.user_data.get("replying_ticket_id")
        if ticket_id:
            reply_text = text
            await asyncio.to_thread(db.reply_ticket, ticket_id, reply_text)
            await asyncio.to_thread(db.log_activity, user_id, "رد على تذكرة", f"Ticket #{ticket_id}")
            await update.message.reply_text("✅ تم إرسال الرد وإغلاق التذكرة.")
            context.user_data.pop("admin_action", None)
            context.user_data.pop("replying_ticket_id", None)
            return

    # معالجة تعديل الإعدادات
    if ADMIN_ID != 0 and user_id == ADMIN_ID and context.user_data.get("admin_action") == "edit_setting":
        key = context.user_data.get("editing_setting_key")
        if key:
            await asyncio.to_thread(db.set_setting, key, text)
            await asyncio.to_thread(db.log_activity, user_id, "تعديل إعداد", f"{key} = {text}")
            await update.message.reply_text(f"✅ تم تحديث {key} إلى {text}")
            context.user_data.pop("admin_action", None)
            context.user_data.pop("editing_setting_key", None)
            return
            
    if ADMIN_ID != 0 and user_id == ADMIN_ID and context.user_data.get("admin_action"):
        action = context.user_data.get("admin_action")
        table_name = get_correct_table_name()
        if action == "add_days":
            try:
                target_id, value = text.split(" ")
                target_id = int(target_id)
                await asyncio.to_thread(db.add_days_to_user, target_id, int(value))
                await asyncio.to_thread(db.log_activity, user_id, "إضافة أيام", f"للمستخدم {target_id} - {value} يوم")
                await update.message.reply_text(f"✅ تم إضافة {value} يوم للمستخدم `{target_id}` بنجاح.")
            except Exception:
                await update.message.reply_text("❌ صيغة خاطئة. يرجى إدخال: `المعرف القيمة` (مثال: `834033986 30`)")
            context.user_data.pop("admin_action", None)
            return
        elif action == "ban":
            try:
                target_id, value = text.split(" ")
                target_id = int(target_id)
                await asyncio.to_thread(db.ban_user, target_id, int(value))
                await asyncio.to_thread(db.log_activity, user_id, "حظر/إلغاء حظر", f"مستخدم {target_id} - حالة {value}")
                status_text = "حظر" if int(value) == 1 else "إلغاء حظر"
                await update.message.reply_text(f"✅ تم تعديل حالة المستخدم `{target_id}` إلى: **{status_text}**.")
            except Exception:
                await update.message.reply_text("❌ صيغة خاطئة. يرجى إدخال: `المعرف القيمة` (مثال للحظر: `834033986 1`)")
            context.user_data.pop("admin_action", None)
            return
        elif action == "delete_user":
            try:
                target_id = int(text)
                try:
                    await bot_manager.stop_bot(target_id)
                except Exception:
                    pass
                def _delete_user():
                    with db.get_connection() as conn:
                        cursor = conn.cursor()
                        try:
                            cursor.execute(f"DELETE FROM {table_name} WHERE user_id = %s", (target_id,))
                            conn.commit()
                        finally:
                            cursor.close()
                await asyncio.to_thread(_delete_user)
                await asyncio.to_thread(db.log_activity, user_id, "حذف مستخدم", f"المستخدم {target_id}")
                await update.message.reply_text(f"🗑️ تم حذف المستخدم `{target_id}` نهائياً من الجدول `{table_name}` وإيقاف خط السحب الخاص به.")
            except Exception as e:
                await update.message.reply_text(f"❌ فشل تنفيذ الحذف. الخطأ: {e}")
            context.user_data.pop("admin_action", None)
            return
        elif action == "add_proxy":
            try:
                lines = text.strip().split('\n')
                added_count = 0
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(",")
                    if len(parts) >= 3:
                        country_code = parts[0].strip().upper()
                        host = parts[1].strip()
                        port = int(parts[2].strip())
                        username = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
                        password = parts[4].strip() if len(parts) > 4 and parts[4].strip() else None
                        provider = parts[5].strip().upper() if len(parts) > 5 and parts[5].strip() else 'STATIC'
                        rotation_url = parts[6].strip() if len(parts) > 6 and parts[6].strip() else None
                        
                        ptype = 'HTTP' if port in (80, 443) else 'SOCKS5'
                        
                        await asyncio.to_thread(
                            db.add_proxy, 
                            country_code, 
                            host, 
                            port, 
                            username, 
                            password, 
                            ptype, 
                            provider, 
                            rotation_url
                        )
                        added_count += 1
                
                if added_count > 0:
                    await asyncio.to_thread(db.log_activity, user_id, "إضافة بروكسيات", f"تم إضافة {added_count} بروكسي")
                    await update.message.reply_text(f"✅ تم إضافة {added_count} بروكسي بنجاح.")
                else:
                    await update.message.reply_text("❌ صيغة خاطئة. لم يتم التعرف على أي بروكسي صالح.")

            except Exception as e:
                await update.message.reply_text(f"❌ حدث خطأ أثناء إضافة البروكسي: {e}")
            context.user_data.pop("admin_action", None)
            return
        elif action == "set_checker_bot":
            bot_username = text.replace("@", "").strip()
            if bot_username.lower() == "off":
                await asyncio.to_thread(db.set_setting, "checker_bot_username", "")
                await update.message.reply_text("❌ تم تعطيل الربط مع بوت الفحص الخارجي.")
            else:
                await asyncio.to_thread(db.set_setting, "checker_bot_username", bot_username)
                await update.message.reply_text(f"✅ تم تفعيل الربط مع البوت: @{bot_username}")
            context.user_data.pop("admin_action", None)
            return
        elif action == "set_manager_acc":
            val = text.strip()
            if val.lower() == "off":
                await asyncio.to_thread(db.set_setting, "external_checker_account_id", "")
                await update.message.reply_text("❌ تم تعطيل ميزة الحساب المدير. سيتم المراسلة من أي حساب فاحص متاح.")
            else:
                try:
                    acc_id = int(val)
                    await asyncio.to_thread(db.set_setting, "external_checker_account_id", str(acc_id))
                    await update.message.reply_text(f"✅ تم تخصيص الحساب رقم `{acc_id}` ليكون حساب المدير (Layer 4).", parse_mode="Markdown")
                except ValueError:
                    await update.message.reply_text("❌ يرجى إرسال أرقام فقط (ID الحساب).")
            context.user_data.pop("admin_action", None)
            return
        elif action == "set_honeypot":
            phone = text.strip()
            if phone.lower() == "off":
                await asyncio.to_thread(db.set_setting, "honeypot_number", "")
                await update.message.reply_text("❌ تم تعطيل رقم الفخ بنجاح.")
            else:
                await asyncio.to_thread(db.set_setting, "honeypot_number", phone)
                await update.message.reply_text(f"✅ تم حفظ رقم الفخ بنجاح: {phone}\n\nسيقوم البوت الآن بفحص هذا الرقم تلقائياً لاختبار الحسابات التي تدعي عدم وجود أرقام الصيد للتأكد من سلامتها من حظر الظل.")
            context.user_data.pop("admin_action", None)
            return


    status_msg = await update.message.reply_text("⏳ جاري التحقق من صحة التوكن المرسل وحفظه...")
    is_valid = await bot_manager.validate_token(text)
    if not is_valid:
        await status_msg.edit_text("❌ التوكن غير صالح! تأكد من الحصول عليه بشكل صحيح من @BotFather.")
        return
    try:
        await asyncio.to_thread(db.save_bot, user_id, text)
        await status_msg.delete()
        await update.message.reply_text("✅ تم شحن وتحديث توكن البوت الجديد بنجاح!")
    except Exception as e:
        await status_msg.edit_text(f"❌ خطأ في قاعدة البيانات: {e}")
        return
    await show_dashboard(update, user_id, user_name)

async def show_dashboard(update: Update, user_id: int, user_name: str):
    # ملاحظة: كنا نعرض "36 يوم" كقيمة افتراضية وهمية حتى عند عدم توفر أي بيانات اشتراك حقيقية،
    # مما يوهم المستخدم برصيد أيام غير موجود فعلياً. الآن نعرض حالة صريحة "غير معروف/لا يوجد اشتراك"
    # بدل رقم مُخترَع يبدو حقيقياً.
    days_left = "غير مشترك"
    status = "⚪️ غير مربوط"
    try:
        db_data = await asyncio.to_thread(db.get_bot, user_id)
        if db_data and len(db_data) >= 4:
            status = bot_manager.get_status(user_id)
            expires_at = db_data[2]
            if expires_at:
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at.replace("Z", ""))
                delta = expires_at.replace(tzinfo=None) - datetime.now(timezone.utc).replace(tzinfo=None)
                days_left = f"{max(0, delta.days)} يوم"
    except Exception as e:
        logger.error(f"[User: {user_id}] فشل جلب بيانات الاشتراك لعرضها في لوحة التحكم: {e}")
        days_left = "تعذر التحقق حالياً"

    text = (
        f"👤 ⪪ حياك الله يا {user_name} 🦾، أهلاً وسهلاً ومرحباً بك.\n\n"
        f"🟢 ⪪ لديك اشتراك نشط، يمكنك هنا تشغيل وإيقاف البوت الخاص بك ⪪ {status}\n\n"
        f"⏰ ⪪ اشتراكك ⪪ {days_left} ⪪"
    )

    keyboard = [
        [InlineKeyboardButton("🔑 توكن البوت", callback_data="show_token_info")],
        [
            InlineKeyboardButton("إيقاف البوت ❌", callback_data="stop_bot"),
            InlineKeyboardButton("تشغيل البوت ✅", callback_data="run_bot")
        ],
        [InlineKeyboardButton("🔄 تجديد الإشتراك", callback_data="renew_subscription")],
        [
            InlineKeyboardButton("تواصل مع الدعم 🤙", callback_data="contact_support"),
            InlineKeyboardButton("بوت فك الحظر ❗️", callback_data="unban_bot")
        ]
    ]
    if ADMIN_ID != 0 and user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👑 لوحة تحكم الإدارة 👑", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID == 0 or update.effective_user.id != ADMIN_ID:
        return
    await show_admin_panel(update)

async def show_admin_panel(update: Update):
    try:
        total, active = await asyncio.to_thread(db.get_stats)
    except Exception:
        total, active = 0, 0

    text = (
        f"👑 **لوحة تحكم المطور الفنية الشاملة** 👑\n\n"
        f"📊 **إحصائيات النظام الفورية:**\n"
        f"👥 إجمالي المستخدمين في النظام: {total}\n"
        f"🚀 البوتات الفرعية النشطة حالياً: {active}\n\n"
        f"⚙️ قم باختيار الإجراء المناسب لإدارة المشتركين والاشتراكات الشهرية:"
    )

    keyboard = [
        [
            InlineKeyboardButton("➕ شحن/تجديد الأيام", callback_data="adm_add_days"),
            InlineKeyboardButton("🚫 حظر / إلغاء حظر", callback_data="adm_ban")
        ],
        [
            InlineKeyboardButton("🗑️ حذف مستخدم نهائياً", callback_data="adm_delete_user"),
            InlineKeyboardButton("🆔 استخراج الـ IDs", callback_data="adm_get_ids")
        ],
        [
            InlineKeyboardButton("👥 إدارة المستخدمين", callback_data="adm_user_management"),
            InlineKeyboardButton("📋 سجل العمليات", callback_data="adm_activity_log")
        ],
        [
            InlineKeyboardButton("🎫 تذاكر الدعم", callback_data="adm_tickets"),
            InlineKeyboardButton("⚙️ إعدادات الأسعار", callback_data="adm_settings")
        ],
        [
            InlineKeyboardButton("⚙️ إضافة حساب فاحص", callback_data="adm_add_checker"),
            InlineKeyboardButton("👥 إدارة الحسابات الفاحصة", callback_data="adm_manage_checkers")
        ],
        [
            InlineKeyboardButton("🌐 إدارة البروكسيات", callback_data="adm_manage_proxies"),
            InlineKeyboardButton("➕ إضافة بروكسي", callback_data="adm_add_proxy")
        ],
        [
            InlineKeyboardButton("🤖 ربط بوت فحص خارجي", callback_data="adm_checker_bot")
        ],
        [
            InlineKeyboardButton("👑 تخصيص حساب المدير (Layer 4)", callback_data="adm_set_manager_acc")
        ],
        [
            InlineKeyboardButton("🎯 إعداد رقم الفخ (Honeypot)", callback_data="adm_set_honeypot")
        ],
        [
            InlineKeyboardButton("⬅️ الواجهة الرئيسية", callback_data="main_menu")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

# ---------- إدارة المستخدمين المتقدمة ----------
async def show_user_management(update: Update, page=0):
    query = update.callback_query
    per_page = 10
    offset = page * per_page
    try:
        def _get_users():
            with db.get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT user_id, token, is_active, expires_at, is_banned FROM user_bots ORDER BY user_id LIMIT %s OFFSET %s", (per_page, offset))
                    return cursor.fetchall()
                finally:
                    cursor.close()
        rows = await asyncio.to_thread(_get_users)
    except Exception as e:
        await query.answer(f"خطأ: {e}", show_alert=True)
        return

    if not rows:
        text = "لا يوجد مستخدمون."
        keyboard = [[InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_panel")]]
    else:
        text = "👥 **قائمة المستخدمين:**\n\n"
        keyboard = []
        for r in rows:
            uid, _, is_active, expires_at, is_banned = r
            status = "🟢 نشط" if is_active else "🔴 متوقف"
            if is_banned:
                status = "🚫 محظور"
            exp = "غير محدد"
            if expires_at:
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at.replace("Z", ""))
                exp = expires_at.strftime("%Y-%m-%d")
            btn_text = f"ID: {uid} - {status} - {exp}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"user_detail_{uid}")])
        # أزرار التنقل بين الصفحات
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"user_page_{page-1}"))
        # بفرض وجود المزيد (فحص بسيط)
        def _get_total():
            with db.get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT COUNT(*) FROM user_bots")
                    return cursor.fetchone()[0]
                finally:
                    cursor.close()
        total = await asyncio.to_thread(_get_total)
        if offset + per_page < total:
            nav_row.append(InlineKeyboardButton("التالي ➡️", callback_data=f"user_page_{page+1}"))
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def show_user_detail(update: Update, user_id: int):
    query = update.callback_query
    data = await asyncio.to_thread(db.get_bot, user_id)
    if not data:
        await query.answer("المستخدم غير موجود", show_alert=True)
        return
    token, is_active, expires_at, is_banned = data
    status = "نشط" if is_active else "متوقف"
    banned = "نعم" if is_banned else "لا"
    exp_text = "غير محدد"
    if expires_at:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace("Z", ""))
        exp_text = expires_at.strftime("%Y-%m-%d %H:%M")
    # عدد الحسابات المربوطة
    accounts = await asyncio.to_thread(db.get_all_site_accounts, user_id)
    num_accounts = len(accounts)
    text = (
        f"👤 **معلومات المستخدم:**\n"
        f"🆔 المعرف: `{user_id}`\n"
        f"📌 الحالة: {status}\n"
        f"🚫 محظور: {banned}\n"
        f"⏰ انتهاء الاشتراك: {exp_text}\n"
        f"🗂 حسابات DurianRCS: {num_accounts}\n"
    )
    keyboard = [
        [InlineKeyboardButton("➕ إضافة أيام", callback_data=f"adm_add_days_{user_id}"),
         InlineKeyboardButton("🚫 حظر/إلغاء", callback_data=f"adm_ban_{user_id}")],
        [InlineKeyboardButton("🗑️ حذف المستخدم", callback_data=f"adm_delete_user_{user_id}")],
        [InlineKeyboardButton("🔙 عودة للقائمة", callback_data="adm_user_management")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

# ---------- سجل العمليات ----------
async def show_activity_log(update: Update):
    query = update.callback_query
    activities = await asyncio.to_thread(db.get_recent_activities, 30)
    if not activities:
        text = "لا توجد عمليات مسجلة حتى الآن."
    else:
        lines = []
        for act in activities:
            _, uid, action, details, ts = act
            ts_str = ts.strftime("%m-%d %H:%M") if ts else ""
            lines.append(f"🕒 {ts_str} | 👤 {uid} | {action} | {details}")
        text = "📋 **آخر العمليات:**\n\n" + "\n".join(lines)
    keyboard = [[InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ---------- تذاكر الدعم ----------
async def show_tickets(update: Update):
    query = update.callback_query
    tickets = await asyncio.to_thread(db.get_open_tickets)
    if not tickets:
        text = "🎫 لا توجد تذاكر مفتوحة."
        keyboard = [[InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_panel")]]
    else:
        text = "🎫 **تذاكر الدعم المفتوحة:**\n\n"
        keyboard = []
        for t in tickets:
            tid, uid, subject, msg, status, reply, created = t
            text += f"🎫 #{tid} | 👤 {uid} | {subject}\n{msg[:50]}...\n\n"
            keyboard.append([InlineKeyboardButton(f"رد / إغلاق #{tid}", callback_data=f"reply_ticket_{tid}")])
        keyboard.append([InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_panel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def handle_reply_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: int):
    query = update.callback_query
    # نطلب من الأدمن إرسال الرد
    context.user_data["replying_ticket_id"] = ticket_id
    await query.message.reply_text("📝 أرسل الآن ردك على هذه التذكرة (نص واحد):")
    # سنحتاج إلى وضع مؤقت لاستقبال الرد. نضيف حالة 'waiting_ticket_reply' في context.user_data
    context.user_data["admin_action"] = "ticket_reply"

# تعديل handle_token (أو إضافة معالج) لاستقبال رد التذكرة
# سنضيف داخل handle_token قبل الأقسام الأخرى:
# if context.user_data.get("admin_action") == "ticket_reply":
#     process ticket reply

# ---------- إعدادات الأسعار والمحافظ ----------
async def show_settings(update: Update):
    query = update.callback_query
    settings = await asyncio.to_thread(db.get_all_settings)
    text = "⚙️ **الإعدادات الحالية:**\n\n"
    keyboard = []
    for k, v in settings:
        text += f"{k}: {v}\n"
        keyboard.append([InlineKeyboardButton(f"✏️ تعديل {k}", callback_data=f"edit_setting_{k}")])
    keyboard.append([InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_panel")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def prompt_edit_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str):
    query = update.callback_query
    context.user_data["editing_setting_key"] = key
    await query.message.reply_text(f"📝 أدخل القيمة الجديدة لـ {key}:")
    context.user_data["admin_action"] = "edit_setting"
async def show_proxy_management(update: Update):
    proxies = await asyncio.to_thread(db.get_all_proxies)
    if not proxies:
        text = "❌ لا توجد بروكسيات مضافة بعد."
        keyboard = [[InlineKeyboardButton("🔙 العودة للوحة الإدارة", callback_data="admin_panel")]]
    else:
        text = "🌐 **قائمة البروكسيات وإحصائيات الجودة:**\n\nاضغط على البروكسي لتغيير حالته (مفعل/معطل)."
        keyboard = []
        for p in proxies:
            status_emoji = "🟢" if p["is_active"] else "🔴"
            provider = p.get("provider", "STATIC")
            success = p.get("success_count", 0)
            failure = p.get("failure_count", 0)
            latency = p.get("avg_latency", 0.0)
            
            btn_text = f"{status_emoji} [{p['country_code']}] {p['host']}:{p['port']} ({provider}) | نجاح:{success} فشل:{failure} بنج:{latency:.2f}s"
            keyboard.append([
                InlineKeyboardButton(btn_text, callback_data=f"toggle_pxy_{p['id']}"),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_pxy_{p['id']}")
            ])
        keyboard.append([InlineKeyboardButton("🔙 العودة للوحة الإدارة", callback_data="admin_panel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def start_add_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id
    if ADMIN_ID == 0 or user_id != ADMIN_ID:
        return
    msg_text = (
        "🌐 **نظام إضافة بروكسي متقدم**\n\n"
        "أرسل بيانات البروكسي بإحدى الصيغ التالية تماماً:\n\n"
        "1. بروكسي بدون كود تدوير:\n"
        "`الدولة,host,port,username,password`\n"
        "أو\n"
        "`الدولة,host,port`\n\n"
        "2. بروكسي مع اسم المزود ورابط التدوير (مثال: Webshare):\n"
        "`الدولة,host,port,username,password,provider,rotation_url`\n\n"
        "مثال:\n"
        "`DE,123.45.67.89,1080,user123,pass456`\n"
        "`FR,12.34.56.78,8080,user,pass,WEBSHARE,https://ipv4.webshare.io/to/rotate`"
    )
    context.user_data["admin_action"] = "add_proxy"
    if query:
        await query.message.reply_text(msg_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_text, parse_mode="Markdown")

# ---------- دوال إدارة الحسابات الفاحصة ----------
async def show_checker_management(update: Update):
    accounts = await asyncio.to_thread(db.get_all_checkers)
    if not accounts:
        text = "❌ لا توجد حسابات فحص مضافة بعد."
        keyboard = [[InlineKeyboardButton("🔙 العودة للوحة الإدارة", callback_data="admin_panel")]]
    else:
        text = "👥 **قائمة حسابات الفحص وإحصائياتها:**\n\n"
        keyboard = []
        for acc_id, phone, is_active, total_checks in accounts:
            status_emoji = "🟢 مفعل" if is_active else "🔴 معطل"
            text += f"▪️ `{phone}` ➜ {status_emoji} | (مفحوص: `{total_checks}`)\n"
            
            btn_emoji = "🟢" if is_active else "🔴"
            btn_text = f"{btn_emoji} {phone}"
            keyboard.append([
                InlineKeyboardButton(btn_text, callback_data=f"toggle_chk_{acc_id}"),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_chk_{acc_id}")
            ])
        text += "\nاضغط على زر الحساب أدناه لتبديل حالته بين تفعيل وتعطيل."
        keyboard.append([InlineKeyboardButton("🔙 العودة للوحة الإدارة", callback_data="admin_panel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def toggle_checker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if ADMIN_ID == 0 or user_id != ADMIN_ID:
        await query.answer("غير مصرح", show_alert=True)
        return
    try:
        acc_id = int(query.data.replace("toggle_chk_", ""))
    except ValueError:
        await query.answer("خطأ في البيانات", show_alert=True)
        return
    accounts = await asyncio.to_thread(db.get_all_checkers)
    acc = next((a for a in accounts if a[0] == acc_id), None)
    if not acc:
        await query.message.reply_text("❌ الحساب غير موجود.")
        return
    phone = acc[1]
    old_status = acc[2]
    await asyncio.to_thread(db.toggle_checker, acc_id)
    new_status = not old_status
    status_text = "تفعيل" if new_status else "تعطيل"
    await query.message.reply_text(f"✅ تم {status_text} الحساب `{phone}` بنجاح.")
    await show_checker_management(update)

# ---------- بقية الدوال ----------
async def force_add_checker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start_add_checker(update, context)

async def start_add_checker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("[TRACE] Conversation started via start_add_checker")
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id
    if ADMIN_ID == 0 or user_id != ADMIN_ID:
        logger.info("[TRACE] Unauthorized access attempt")
        return ConversationHandler.END
    msg_text = (
        "🚀 **نظام ربط حساب الفحص التلقائي**\n\n"
        "أرسل بيانات الحساب الفاحص بالصيغة التالية تماماً:\n"
        "`الرقم,api_id,api_hash`\n\n"
        "مثال:\n"
        "`+967777777777,28412234,b3a6c98ea...`"
    )
    if query:
        await query.message.reply_text(msg_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_text, parse_mode="Markdown")
    logger.info("[TRACE] State returned: PHONE")
    return PHONE

async def get_phone_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    logger.info(f"[TRACE] Checker state received message (PHONE state): {text}")
    try:
        phone, api_id, api_hash = text.split(",")
        context.user_data["chk_phone"] = phone.strip()
        context.user_data["chk_api_id"] = api_id.strip()
        context.user_data["chk_api_hash"] = api_hash.strip()
        await update.message.reply_text("⏳ جاري الاتصال بخوادم التليجرام وإرسال كود التحقق...")
        await login_manager.send_code(
            context.user_data["chk_phone"],
            context.user_data["chk_api_id"],
            context.user_data["chk_api_hash"]
        )
        await update.message.reply_text("💬 وصلك كود الآن على حساب التليجرام الفاحص، يرجى إرساله هنا فوراً:")
        logger.info("[TRACE] State returned: CODE")
        return CODE
    except Exception as e:
        await update.message.reply_text(f"❌ فشل الاتصال بالحساب أو أن الصيغة غير صحيحة.\nالخطأ: `{str(e)}`")
        return PHONE

async def get_code_and_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    logger.info(f"[TRACE] Checker state received message (CODE state): {code}")
    phone = context.user_data["chk_phone"]
    await update.message.reply_text("⏳ جاري التحقق من كود تسجيل الدخول...")
    try:
        result = await login_manager.verify_code(phone, code)
        if result.get("status") == "CODE_EXPIRED":
            await update.message.reply_text("⏳ انتهت صلاحية الكود. تم إرسال كود جديد إلى رقمك. أرسل الكود الجديد:")
            return CODE  # يجب أن يعود إلى نفس الحالة لاستقبال الكود الجديد
        if result.get("status") == "PASSWORD_REQUIRED":
            await update.message.reply_text("🔒 هذا الحساب محمي بالتحقق بخطوتين، من فضلك أرسل باسوورد الحساب الآن:")
            return PASSWORD
        if result.get("status") == "SUCCESS":
            await update.message.reply_text(f"✅ تم ربط الحساب الفاحص بنجاح!\n👤 الاسم: {result.get('name')}")
            await login_manager.cleanup()
            return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ برميجي أثناء تفعيل الكود: `{str(e)}`")
        await login_manager.cleanup()
        return ConversationHandler.END
        
async def get_password_and_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    logger.info(f"[TRACE] Checker state received message (PASSWORD state)")
    phone = context.user_data["chk_phone"]
    await update.message.reply_text("⏳ جاري فك التحقق بخطوتين وتخزين الجلسة...")
    try:
        result = await login_manager.verify_password(phone, password)
        if result.get("status") == "SUCCESS":
            await update.message.reply_text(f"✅ تم تخطّي كلمة المرور بنجاح وحفظ الحساب الفاحص!\n👤 الاسم: {result.get('name')}")
    except Exception as e:
        await update.message.reply_text(f"❌ كلمة المرور خاطئة أو انتهت مهلة الجلسة: `{str(e)}`")
    finally:
        await login_manager.cleanup()
    logger.info("[TRACE] Conversation ended (PASSWORD SUCCESS/FAIL)")
    return ConversationHandler.END

async def cancel_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await login_manager.cleanup()
    await update.message.reply_text("❌ تم إلغاء عملية ربط الحساب الفاحص بنجاح.")
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_name = query.from_user.first_name

    # ---------- قسم الأدمن ----------
    if ADMIN_ID != 0 and user_id == ADMIN_ID:
        if query.data == "admin_panel":
            await show_admin_panel(update)
            return
        elif query.data == "adm_manage_checkers":
            await show_checker_management(update)
            return
        elif query.data.startswith("delete_chk_"):
            acc_id = int(query.data.replace("delete_chk_", ""))
            await asyncio.to_thread(db.delete_checker, acc_id)
            await query.answer("🗑️ تم حذف الحساب الفاحص", show_alert=False)
            await show_checker_management(update)
            return
        elif query.data.startswith("toggle_chk_"):
            await toggle_checker_callback(update, context)
            return
        elif query.data == "adm_manage_proxies":
            await show_proxy_management(update)
            return
        elif query.data == "adm_add_proxy":
            await start_add_proxy(update, context)
            return
        elif query.data == "adm_checker_bot":
            context.user_data["admin_action"] = "set_checker_bot"
            await query.message.reply_text(
                "🤖 **نظام ربط بوت فحص خارجي**\n\n"
                "الرجاء إرسال معرف البوت. مثال:\n"
                "`SessionCheckerReBoT`\n\n"
                "لإلغاء التفعيل أرسل: `off`",
                parse_mode="Markdown"
            )
            return
        elif query.data == "adm_set_manager_acc":
            context.user_data["admin_action"] = "set_manager_acc"
            # Get all accounts to show IDs
            try:
                from telegram_checker.account_manager import account_manager
                accounts = await account_manager.get_all_accounts()
                msg = "👑 **تخصيص حساب المدير للطبقة الرابعة**\n\n"
                msg += "هذا الحساب سيتولى حصرياً مراسلة البوت الخارجي.\n"
                msg += "الحسابات المتاحة:\n"
                for acc in accounts:
                    msg += f"ID: `{acc['id']}` - Number: {acc['phone']}\n"
                msg += "\nالرجاء إرسال ID الحساب المطلوب كمدير، أو أرسل `off` لتعطيل الميزة وجعل أي حساب يراسل البوت."
                await query.message.reply_text(msg, parse_mode="Markdown")
            except Exception as e:
                await query.message.reply_text(f"خطأ: {e}")
            return
        elif query.data == "adm_set_honeypot":
            context.user_data["admin_action"] = "set_honeypot"
            await query.message.reply_text(
                "🎯 **إعداد رقم الفخ (Honeypot)**\n\n"
                "الرجاء إرسال الرقم الذي تريد استخدامه كفخ لاختبار جودة الحسابات الفاحصة وكشف حظر الظل. مثال:\n"
                "`+123456789`\n\n"
                "لإلغاء التفعيل أرسل: `off`",
                parse_mode="Markdown"
            )
            return
        elif query.data.startswith("delete_pxy_"):
            pxy_id = int(query.data.replace("delete_pxy_", ""))
            await asyncio.to_thread(db.delete_proxy, pxy_id)
            await query.answer("🗑️ تم حذف البروكسي بنجاح", show_alert=False)
            await show_proxy_management(update)
            return
        elif query.data.startswith("toggle_pxy_"):
            pxy_id = int(query.data.replace("toggle_pxy_", ""))
            proxies = await asyncio.to_thread(db.get_all_proxies)
            pxy = next((p for p in proxies if p["id"] == pxy_id), None)
            if pxy:
                new_status = not pxy["is_active"]
                await asyncio.to_thread(db.toggle_proxy, pxy_id, new_status)
                status_text = "تفعيل" if new_status else "تعطيل"
                await query.answer(f"✅ تم {status_text} البروكسي", show_alert=True)
            await show_proxy_management(update)
            return
        elif query.data == "adm_add_days":
            context.user_data["admin_action"] = "add_days"
            await query.message.reply_text("📥 أرسل معرف المستخدم وعدد الأيام مفصولين بمسافة:\nمثال: `834033986 30`")
            return
        elif query.data == "adm_ban":
            context.user_data["admin_action"] = "ban"
            await query.message.reply_text("📥 أرسل معرف المستخدم وحالته مفصولين بمسافة:\n(`1` للحظر أو `0` لإلغاء الحظر)\nمثال: `834033986 1`")
            return
        elif query.data == "adm_delete_user":
            context.user_data["admin_action"] = "delete_user"
            await query.message.reply_text("🗑️ أرسل `ID المستخدم` المراد مسحه تماماً من السيرفر وإلغاء بوتاته:")
            return
        elif query.data == "adm_get_ids":
            try:
                table_name = get_correct_table_name()
                def _get_ids():
                    with db.get_connection() as conn:
                        cursor = conn.cursor()
                        try:
                            cursor.execute(f"SELECT user_id FROM {table_name}")
                            return cursor.fetchall()
                        finally:
                            cursor.close()
                rows = await asyncio.to_thread(_get_ids)
                if rows:
                    user_list = "\n".join([f"👤 ID: `{row[0]}`" for row in rows])
                else:
                    user_list = "لا يوجد مستخدمين مسجلين حالياً."
                text = f"🆔 **قائمة معرّفات مستخدمي النظام التفصيلية:**\n*(تم القراءة من جدول: `{table_name}`)*\n\n{user_list}"
            except Exception as e:
                text = f"❌ تعذر استخراج المعرفات تلقائياً: {e}"
            keyboard = [[InlineKeyboardButton("🔙 العودة للوحة الإدارة", callback_data="admin_panel")]]
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return

        elif query.data == "adm_user_management":
            await show_user_management(update)
            return
        elif query.data.startswith("user_page_"):
            page = int(query.data.split("_")[-1])
            await show_user_management(update, page)
            return
        elif query.data.startswith("user_detail_"):
            uid = int(query.data.split("_")[-1])
            await show_user_detail(update, uid)
            return
        elif query.data == "adm_activity_log":
            await show_activity_log(update)
            return
        elif query.data == "adm_tickets":
            await show_tickets(update)
            return
        elif query.data.startswith("reply_ticket_"):
            tid = int(query.data.split("_")[-1])
            await handle_reply_ticket(update, context, tid)
            return
        elif query.data == "adm_settings":
            await show_settings(update)
            return
        elif query.data.startswith("edit_setting_"):
            key = query.data[len("edit_setting_"):]
            await prompt_edit_setting(update, context, key)
            return
        # زر تأكيد الدفع (للأدمن فقط)
        elif query.data.startswith("confirm_pay_"):
            if user_id != ADMIN_ID:
                await query.answer("غير مصرح", show_alert=True)
                return
            target_id = int(query.data.split("_")[2])
            # نستخدم claim_pending_subscription (قراءة + حذف ذريّان معاً عبر DELETE...RETURNING)
            # بدل get ثم delete كخطوتين منفصلتين، لمنع تفعيل نفس الاشتراك مرتين لو ضغط الأدمن
            # زر التأكيد مرتين بسرعة (Race Condition).
            pending = await asyncio.to_thread(db.claim_pending_subscription, target_id)
            if not pending:
                await query.answer("لا يوجد طلب معلق (ربما تم تأكيده بالفعل).", show_alert=True)
                return
            plan, method, amount, wallet, _ = pending

            if "حسابين" in plan:
                plan_num = "2"
            elif "3 حسابات" in plan:
                plan_num = "3"
            else:
                plan_num = "1"

            await asyncio.to_thread(db.add_days_to_user, target_id, 30, plan_type=plan_num)
            await asyncio.to_thread(db.log_activity, target_id, "تفعيل اشتراك", f"خطة {plan} - 30 يوم")
            await asyncio.to_thread(db.log_activity, ADMIN_ID, "تأكيد دفع", f"مستخدم {target_id} - خطة {plan}")
            new_data = await asyncio.to_thread(db.get_bot, target_id)
            if new_data:
                expires_at = new_data[2]
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at.replace("Z", ""))
                expiry_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
                success_msg = (
                    f"🗒 ❛ ≽ تم دفع الفاتورة بنجاح.. ≼\n\n"
                    f"🔋 ❛ نوعية الإشتراك ≽ DurianRCS ({plan}) ≼\n"
                    f"⏰ ❛ الأيام المضافة ≽ 30 يوم ≼\n"
                    f"⏰ ❛ اشتراكك الجديد ينتهي في ≽ {expiry_str} ≼"
                )
                try:
                    await context.bot.send_message(chat_id=target_id, text=success_msg)
                except Exception as e:
                    logger.error(f"فشل إرسال رسالة نجاح الدفع للمستخدم {target_id}: {e}")
                    await query.answer("تم تأكيد الدفع ولكن تعذر إرسال إشعار للمستخدم.", show_alert=True)
                    return
                await query.answer("تم تأكيد الدفع وإرسال الإشعار.", show_alert=True)
            else:
                await query.answer("المستخدم غير موجود.", show_alert=True)
            await query.message.edit_text(query.message.text + "\n\n✅ تم التأكيد.")
            return

    # ---------- أزرار الاشتراك للمستخدمين الجدد ----------
    elif query.data == "subscribe":
        keyboard = [
            [InlineKeyboardButton("DurianRCS (حساب واحد) - 4$", callback_data="plan_1")],
            [InlineKeyboardButton("DurianRCS (حسابين) - 6$", callback_data="plan_2")],
            [InlineKeyboardButton("DurianRCS (3 حسابات) - 8$", callback_data="plan_3")],
        ]
        await query.message.edit_text("اختر خطة الاشتراك:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif query.data.startswith("plan_"):
        plan_num = query.data.split("_")[1]
        context.user_data["selected_plan"] = plan_num
        prices = {"1": "4", "2": "6", "3": "8"}
        price = prices[plan_num]
        text = f"📋 اختر طريقة الدفع لـ DurianRCS ({'حساب واحد' if plan_num=='1' else 'حسابين' if plan_num=='2' else '3 حسابات'}):\n🔹 قيمة الاشتراك : {price}$"
        keyboard = [
            [InlineKeyboardButton(f"الدفع ب USDT (Binance)", callback_data=f"pay_usdt_{plan_num}")],
            [InlineKeyboardButton(f"الدفع ب TRX (Tron)", callback_data=f"pay_trx_{plan_num}")],
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif query.data.startswith("pay_"):
        _, method, plan_num = query.data.split("_")
        method = method.upper()

        # --- جلب الأسعار والمحافظ من الإعدادات الديناميكية ---
        plan_price, usdt_rate, trx_rate, usdt_wallet, trx_wallet = await asyncio.gather(
            asyncio.to_thread(db.get_setting, f'plan_price_{plan_num}', '0'),
            asyncio.to_thread(db.get_setting, 'usdt_rate', '1'),
            asyncio.to_thread(db.get_setting, 'trx_rate', '0.16'),
            asyncio.to_thread(db.get_setting, 'usdt_wallet', 'TYourUSDTAddressHere'),
            asyncio.to_thread(db.get_setting, 'trx_wallet', 'TSDqje1oWAcDY8Q5XzUDLWksWMSPqxv3PB'),
        )
        plan_price = float(plan_price)
        usdt_rate = float(usdt_rate)
        trx_rate = float(trx_rate)

        # حساب المبلغ بالعملة الرقمية بناءً على سعر الصرف
        if method == "USDT":
            amount = round(plan_price / usdt_rate, 2) if usdt_rate else plan_price
            wallet = usdt_wallet
            currency = "USDT"
        else:  # TRX
            amount = round(plan_price / trx_rate, 2) if trx_rate else plan_price * 6.25
            wallet = trx_wallet
            currency = "TRX"

        plan_name = "حساب واحد" if plan_num == "1" else "حسابين" if plan_num == "2" else "3 حسابات"

        await asyncio.to_thread(db.add_pending_subscription, user_id, plan_name, currency, amount, wallet)

        # إرسال إشعار للإدارة
        admin_msg = (
            f"🔔 طلب اشتراك جديد:\n"
            f"👤 المستخدم: `{user_id}`\n"
            f"📦 الخطة: {plan_name}\n"
            f"💲 المبلغ: {amount} {currency}\n"
            f"📅 الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        admin_keyboard = [[InlineKeyboardButton("✅ تأكيد الدفع", callback_data=f"confirm_pay_{user_id}")]]
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, reply_markup=InlineKeyboardMarkup(admin_keyboard))

        # رد للمستخدم
        text = (
            f"💰 لإيداع {amount} {currency}، يرجى إرسال المبلغ إلى العنوان التالي خلال 10 دقائق:\n\n"
            f"<code>{wallet}</code>\n\n"
            f"✅ سيتم تفعيل الاشتراك فورًا بعد وصول {amount} {currency}"
        )
        try:
            await query.message.edit_text(text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"فشل تحرير رسالة الدفع: {e}")
            await query.message.reply_text(text, parse_mode="HTML")
        return
    # ---------- باقي الأزرار العامة ----------
    if query.data == "main_menu":
        await show_dashboard(update, user_id, user_name)
        return

    db_data = await asyncio.to_thread(db.get_bot, user_id)
    token = db_data[0] if (db_data and len(db_data) > 0) else None

    if query.data == "show_token_info":
        if not token:
            text = (
                f"📝 يرجى إرسال توكن البوت فقط:\n\n"
                f"مثال:\n"
                f"<code>123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew</code>"
            )
            await query.message.reply_text(text, parse_mode="HTML")
            # نجعل البوت ينتظر التوكن الجديد
            context.user_data["waiting_for_token"] = True
        else:
            await query.message.reply_text(f"🔑 توكنك المسجل الحالي هو:\n<code>{token}</code>", parse_mode="HTML")
        return
    elif query.data == "run_bot":
        if not token:
            await query.message.reply_text("⚠️ يرجى إرسال توكن البوت أولاً لربطه.")
        else:
            # جلب عدد الحسابات النشطة لمعرفة نوع البوت
            active_accounts = await asyncio.to_thread(db.get_active_site_accounts, user_id)
            num_accounts = len(active_accounts) if active_accounts else 0
            if num_accounts == 1:
                bot_type = "حساب واحد"
            elif num_accounts == 2:
                bot_type = "حسابين"
            elif num_accounts >= 3:
                bot_type = f"{num_accounts} حسابات"
            else:
                bot_type = "غير محدد"
            
            success = await bot_manager.start_bot(user_id, token)
            if success:
                text = (
                    f"✅ تم تشغيل البوت بنجاح!\n"
                    f"🔹 نوع البوت: DurianRCS ({bot_type})\n\n"
                    f"⚠️ اذا كان بوتك DURIAN ولم يتم تشغيل البوت انتظر 5 دقائق لا تقم بإيقافه"
                )
                await query.message.reply_text(text)
            else:
                await query.message.reply_text("ℹ️ البوت الفرعي يعمل بالفعل في الخلفية.")
            await show_dashboard(update, user_id, user_name)
    elif query.data == "stop_bot":
        if not token:
            await query.message.reply_text("❌ ليس لديك بوت نشط لإيقافه.")
        else:
            await query.message.reply_text("⏳ جاري إيقاف البوت، يرجى الانتظار...")
            await bot_manager.stop_bot(user_id)
            await query.message.reply_text("🛑 تم إيقاف البوت بنجاح.")
            await show_dashboard(update, user_id, user_name)
    elif query.data == "renew_subscription":
        await query.message.reply_text(
            f"⚙️ **لتجديد اشتراكك الشهري:**\n\n"
            f"يرجى التواصل مع الإدارة مباشرة وتزويدهم بالمعرف الخاص بك لتفعيل باقتك:\n"
            f"🆔 معرف حسابك: `{user_id}`", parse_mode="Markdown"
        )
    elif query.data in ["contact_support", "unban_bot"]:
        await query.message.reply_text("ℹ️ هذا الخيار قيد التهيئة الفنية حالياً.")

async def safe_restore():
    try:
        await bot_manager.restore_active_bots()
    except Exception as e:
        logger.error(f"Error restoring bots on startup: {e}")

async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """
    معالج أخطاء عام: بدونه، أي استثناء غير متوقع خارج try/except المحلية
    كان يُعالَج داخلياً بصمت بواسطة PTB دون أي تنبيه فعلي للأدمن (ضعف Observability).
    """
    logger.error(f"Unhandled exception in main bot: {context.error}", exc_info=context.error)
    if ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ خطأ غير متوقع في البوت الرئيسي:\n\n{context.error}"
            )
        except Exception:
            pass

async def main():
    try:
        db.init_db()
    except Exception as e:
        logger.error(f"Database init error: {e}")

    request_config = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=5.0,
        connection_pool_size=16,  # اتصالات HTTP دائمة مع Telegram API
    )
    main_app = (
        Application.builder()
        .token(MAIN_TOKEN)
        .request(request_config)
        .concurrent_updates(True)   # معالجة طلبات متعددة بشكل متوازٍ
        .build()
    )

    checker_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add_checker", force_add_checker),
            CallbackQueryHandler(start_add_checker, pattern="^adm_add_checker$")
        ],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone_and_send)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_code_and_verify)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password_and_verify)],
        },
        fallbacks=[CommandHandler("cancel", cancel_process)],
        per_message=False
    )

    logger.info("[TRACE] Registering checker_conv")
    main_app.add_handler(checker_conv)

    main_app.add_handler(CommandHandler("start", start))
    main_app.add_handler(CommandHandler("set_honeypot", set_honeypot))
    main_app.add_handler(CommandHandler("set_checker_bot", set_checker_bot))
    main_app.add_handler(CommandHandler("admin", admin_command))
    main_app.add_handler(CallbackQueryHandler(button_handler))

    async def debug_handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"[TRACE] handle_token RECEIVED message: '{update.message.text}'")
        return await handle_token(update, context)

    main_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, debug_handle_token))
    main_app.add_error_handler(global_error_handler)

    await main_app.initialize()
    await main_app.updater.start_polling()
    await main_app.start()

    async def proxy_health_checker_loop():
        await asyncio.sleep(60)  # الانتظار دقيقة عند بدء التشغيل لعدم التضارب
        while True:
            try:
                await check_all_proxies_health()
            except Exception as e:
                logger.error(f"[BackgroundTasks] Error in proxy health checker loop: {e}")
            await asyncio.sleep(600)  # الفحص كل 10 دقائق

    asyncio.create_task(safe_restore())
    asyncio.create_task(proxy_health_checker_loop())

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        await main_app.updater.stop()
        await main_app.stop()
        await main_app.shutdown()

if __name__ == '__main__':
    asyncio.run(main())
