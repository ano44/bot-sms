import httpx
import logging
import asyncio
import re

logger = logging.getLogger(__name__)

BASE_URL = "https://api.durianrcs.com/out/ext_api"

# Global AsyncClient to reuse TCP connections (keep-alive)
_http_client = httpx.AsyncClient(timeout=30)

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.5


def _mask_url(url: str) -> str:
    """يُخفي قيمة ApiKey في نص الـ URL قبل تسجيله في اللوج."""
    return re.sub(r"(ApiKey=)[^&]+", r"\1***MASKED***", url)


async def _get_with_retry(url: str):
    """طلب GET مع إعادة محاولة وBackoff تصاعدي."""
    last_exc = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            response = await _http_client.get(url)
            if response.status_code >= 500 and attempt < _RETRY_ATTEMPTS:
                logger.warning(f"DurianAPI: استجابة {response.status_code}, إعادة محاولة {attempt}/{_RETRY_ATTEMPTS}...")
                await asyncio.sleep(_RETRY_BASE_DELAY * attempt)
                continue
            return response
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
            last_exc = e
            if attempt < _RETRY_ATTEMPTS:
                logger.warning(f"DurianAPI: فشل الاتصال ({e}), إعادة محاولة {attempt}/{_RETRY_ATTEMPTS}...")
                await asyncio.sleep(_RETRY_BASE_DELAY * attempt)
            else:
                raise
    if last_exc:
        raise last_exc


class DurianAPI:

    # ==================== 1. معلومات المستخدم والرصيد ====================

    @staticmethod
    async def get_user_info(username: str, api_key: str) -> dict:
        """جلب معلومات المستخدم الكاملة - getUserInfo"""
        url = f"{BASE_URL}/getUserInfo?name={username}&ApiKey={api_key}"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200 and "data" in data:
                    return {"status": "success", "data": data["data"]}
                else:
                    return {"status": "error", "code": data.get("code"), "message": data.get("msg", "خطأ غير معروف")}
        except Exception as e:
            logger.error(f"Error getting user info for {username}: {e}")
        return {"status": "error", "message": "فشل الاتصال بالسيرفر"}

    @staticmethod
    async def get_balance_by_name(username: str, api_key: str) -> float:
        """جلب رصيد الحساب - getUserInfo"""
        result = await DurianAPI.get_user_info(username, api_key)
        if result.get("status") == "success":
            return float(result["data"].get("score", 0.0))
        return 0.0

    @staticmethod
    async def get_balance(username: str, api_key: str) -> float:
        """جلب رصيد الحساب الحقيقي من واجهة البرمجة"""
        return await DurianAPI.get_balance_by_name(username, api_key)

    # ==================== 2. الحصول على رقم هاتف ====================

    @staticmethod
    async def order_number_by_name(username: str, api_key: str, country_code: str, project_id: str = "0257") -> dict:
        """طلب سحب رقم - getMobile (serial=2 للرقم الفردي)"""
        url = (
            f"{BASE_URL}/getMobile?name={username}&ApiKey={api_key}"
            f"&cuy={country_code}&pid={project_id}&num=1&noblack=0&serial=2"
        )
        logger.info(f"[TRACE] DurianAPI.order_number: URL={_mask_url(url)}")
        try:
            response = await _get_with_retry(url)
            logger.info(f"[TRACE] DurianAPI Response: Status={response.status_code}, Body={response.text}")
            if response.status_code == 200:
                data = response.json()
                code = data.get("code")
                if code == 200:
                    phone = data.get("data")
                    logger.info(f"[TRACE] DurianAPI Success: Number={phone}")
                    return {"status": "success", "number": phone}
                elif code == 409:
                    logger.warning(f"[TRACE] DurianAPI Rate Limit (409): {data.get('msg')}")
                    return {"status": "rate_limit", "message": data.get("msg", "تردد الطلبات مرتفع جداً")}
                elif code == 403:
                    return {"status": "no_balance", "message": "رصيد غير كافٍ"}
                elif code == 906:
                    return {"status": "no_numbers", "message": "لا توجد أرقام متاحة لهذه الدولة"}
                else:
                    logger.warning(f"[TRACE] DurianAPI Logic Error: Code={code}, Msg={data.get('msg')}")
                    return {"status": "error", "code": code, "message": data.get("msg", "خطأ غير معروف")}
            else:
                logger.error(f"[TRACE] DurianAPI HTTP Error: Status={response.status_code}")
        except Exception as e:
            logger.error(f"[TRACE] DurianAPI Exception: {str(e)}", exc_info=True)
        return {"status": "error", "message": "فشل الاتصال بالسيرفر"}

    # ==================== 3. جلب كود التحقق ====================

    @staticmethod
    async def get_sms(username: str, api_key: str, phone_number: str, project_id: str = "0257") -> dict:
        """جلب كود التحقق - getMsg"""
        url = f"{BASE_URL}/getMsg?name={username}&ApiKey={api_key}&pn={phone_number}&pid={project_id}&serial=2"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                code = data.get("code")
                if code == 200:
                    return {"status": "success", "sms": data.get("data")}
                elif code == 908:
                    return {"status": "waiting", "message": "الرسالة لم تصل بعد، حاول مجدداً"}
                elif code == 407:
                    return {"status": "all_sms", "message": data.get("msg", "جميع الرسائل")}
                else:
                    return {"status": "waiting", "message": data.get("msg", "قيد الانتظار")}
            else:
                logger.warning(f"getMsg failed: status={response.status_code}, body={response.text}")
                return {"status": "error", "message": "فشل الاتصال بالسيرفر"}
        except Exception as e:
            logger.error(f"Error getting SMS for {phone_number}: {e}")
        return {"status": "error", "message": "فشل الاتصال بالسيرفر"}

    # ==================== 4. تحرير رقم (Release) ====================

    @staticmethod
    async def cancel_number(username: str, api_key: str, phone_number: str, project_id: str = "0257") -> bool:
        """تحرير الرقم وإعادته للمنصة - passMobile"""
        url = f"{BASE_URL}/passMobile?name={username}&ApiKey={api_key}&pn={phone_number}&pid={project_id}&serial=2"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                code = data.get("code")
                if code == 200:
                    logger.info(f"Successfully released number {phone_number}")
                    return True
                else:
                    logger.warning(f"passMobile failed: Code={code}, Msg={data.get('msg')}")
                    return False
            else:
                logger.warning(f"passMobile failed: status={response.status_code}, body={response.text}")
                return False
        except Exception as e:
            logger.error(f"Error canceling number {phone_number}: {e}")
            return False

    # ==================== 5. إضافة رقم للقائمة السوداء ====================

    @staticmethod
    async def add_blacklist(username: str, api_key: str, phone_number: str, project_id: str = "0257") -> bool:
        """إضافة رقم إلى القائمة السوداء - addBlack"""
        url = f"{BASE_URL}/addBlack?name={username}&ApiKey={api_key}&pn={phone_number}&pid={project_id}"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                code = data.get("code")
                # 200: نجح، 912: الرقم موجود أصلاً في القائمة (كلاهما يُعتبر نجاحاً)
                if code in (200, 912):
                    return True
                else:
                    logger.warning(f"addBlack failed: Code={code}, Msg={data.get('msg')}")
                    return False
        except Exception as e:
            logger.error(f"Error adding {phone_number} to blacklist: {e}")
        return False

    # ==================== 6. فحص حالة الرقم ====================

    @staticmethod
    async def get_number_status(username: str, api_key: str, phone_number: str, project_id: str = "0257") -> dict:
        """فحص حالة الرقم على منصة DurianRCS - getStatus
        
        رموز الإرجاع:
        201: تم استقبال SMS بنجاح
        202: الرقم محجوز، لم يصل SMS
        203: الرقم غير محجوز، لم يصل SMS
        """
        url = f"{BASE_URL}/getStatus?name={username}&ApiKey={api_key}&pn={phone_number}&pid={project_id}"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                code = data.get("code")
                if code == 201:
                    return {"status": "sms_received", "code": 201, "message": data.get("msg", "تم استقبال SMS")}
                elif code == 202:
                    return {"status": "occupied_no_sms", "code": 202, "message": data.get("msg", "محجوز، SMS لم يصل")}
                elif code == 203:
                    return {"status": "free_no_sms", "code": 203, "message": data.get("msg", "غير محجوز")}
                else:
                    return {"status": "error", "code": code, "message": data.get("msg", "خطأ")}
        except Exception as e:
            logger.error(f"Error getting status for {phone_number}: {e}")
        return {"status": "error", "message": "فشل الاتصال بالسيرفر"}

    # ==================== 7. التحقق من القائمة السوداء ====================

    @staticmethod
    async def get_blacklist_status(username: str, api_key: str, phone_number: str, project_id: str = "0257") -> dict:
        """التحقق من وجود الرقم في القائمة السوداء - getBlack
        
        رموز الإرجاع:
        200100: الرقم في القائمة السوداء
        400100: الرقم ليس في القائمة السوداء
        """
        url = f"{BASE_URL}/getBlack?name={username}&ApiKey={api_key}&pn={phone_number}&pid={project_id}"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                code = data.get("code")
                if code == 200100:
                    return {"status": "blacklisted", "message": "الرقم في القائمة السوداء"}
                elif code == 400100:
                    return {"status": "not_blacklisted", "message": "الرقم ليس في القائمة السوداء"}
                else:
                    return {"status": "error", "code": code, "message": data.get("msg", "خطأ")}
        except Exception as e:
            logger.error(f"Error checking blacklist for {phone_number}: {e}")
        return {"status": "error", "message": "فشل الاتصال بالسيرفر"}

    # ==================== 8. إحصائيات الأرقام المتاحة حسب الدولة ====================

    @staticmethod
    async def get_country_stats(username: str, api_key: str, project_id: str = "null", vip: str = "null") -> dict:
        """استعلام توزيع الأرقام المتاحة حسب الدولة - getCountryPhoneNum"""
        url = f"{BASE_URL}/getCountryPhoneNum?name={username}&ApiKey={api_key}&pid={project_id}&vip={vip}"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                code = data.get("code")
                if code == 200:
                    return {"status": "success", "data": data.get("data", {})}
                elif code == 403:
                    return {"status": "no_data", "data": {}}
                else:
                    return {"status": "error", "code": code, "message": data.get("msg", "خطأ")}
        except Exception as e:
            logger.error(f"Error getting country stats: {e}")
        return {"status": "error", "message": "فشل الاتصال بالسيرفر"}
