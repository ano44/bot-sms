import httpx
import logging
import asyncio
import re

logger = logging.getLogger(__name__)

BASE_URL = "https://api.durianrcs.com/out/ext_api"

# Global AsyncClient to reuse TCP connections (keep-alive)
_http_client = httpx.AsyncClient(timeout=30)

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.5  # ثانية، مع Backoff تصاعدي بسيط


def _mask_url(url: str) -> str:
    """يُخفي قيمة ApiKey في نص الـ URL قبل تسجيله في اللوج، لمنع تسريب المفتاح في السجلات."""
    return re.sub(r"(ApiKey=)[^&]+", r"\1***MASKED***", url)


async def _get_with_retry(url: str):
    """طلب GET مع إعادة محاولة (Retry) وBackoff تصاعدي عند فشل الاتصال أو استجابات السيرفر (5xx)."""
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
    @staticmethod
    async def get_balance_by_name(username: str, api_key: str) -> float:
        """جلب رصيد الحساب - getUserInfo"""
        url = f"{BASE_URL}/getUserInfo?name={username}&ApiKey={api_key}"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200 and "data" in data:
                    user_data = data["data"]
                    return float(user_data.get("score", 0.0))
        except Exception as e:
            logger.error(f"Error checking balance: {e}")
        return 0.0

    @staticmethod
    async def order_number_by_name(username: str, api_key: str, country_code: str, project_id: str = "0257") -> dict:
        """طلب سحب رقم - getMobile"""
        url = (
            f"{BASE_URL}/getMobile?name={username}&ApiKey={api_key}"
            f"&cuy={country_code}&pid={project_id}&num=1&noblack=0&serial=2"
        )
        logger.info(f"[TRACE] DurianAPI.order_number_by_name: Final URL generated: {_mask_url(url)}")
        try:
            response = await _get_with_retry(url)
            logger.info(f"[TRACE] DurianAPI Response: Status={response.status_code}, RawBody={response.text}")
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200:
                    extracted_num = data.get("data")
                    logger.info(f"[TRACE] DurianAPI Success: Extracted Number: {extracted_num}")
                    return {"status": "success", "number": extracted_num}
                else:
                    logger.warning(f"[TRACE] DurianAPI Logic Error: Code={data.get('code')}, Msg={data.get('msg')}")
                    return {"status": "error", "message": data.get("msg", "Unknown error")}
            else:
                logger.error(f"[TRACE] DurianAPI HTTP Error: Status={response.status_code}")
        except Exception as e:
            logger.error(f"[TRACE] DurianAPI Exception: {str(e)}", exc_info=True)
        return {"status": "error", "message": "Connection failed"}

    @staticmethod
    async def get_sms(username: str, api_key: str, phone_number: str, project_id: str = "0257") -> dict:
        """جلب كود التحقق - getMsg (المُصححة من الوثيقة v2.0)"""
        # استخدام المعامل pn بدلاً من phone، والنقطة getMsg بدلاً من getSms
        url = f"{BASE_URL}/getMsg?name={username}&ApiKey={api_key}&pn={phone_number}&pid={project_id}&serial=2"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200:
                    return {"status": "success", "sms": data.get("data")}
                else:
                    return {"status": "waiting", "message": data.get("msg", "قيد الانتظار")}
            else:
                logger.warning(f"getMsg failed: status={response.status_code}, body={response.text}")
                return {"status": "error", "message": "فشل الاتصال بالسيرفر"}
        except Exception as e:
            logger.error(f"Error getting SMS for {phone_number}: {e}")
        return {"status": "error", "message": "فشل الاتصال بالسيرفر"}

    @staticmethod
    async def cancel_number(username: str, api_key: str, phone_number: str, project_id: str = "0257") -> bool:
        """تحرير الرقم - passMobile (المُصححة من الوثيقة v2.0)"""
        # استخدام passMobile بدلاً من cancelMobile، مع المعاملات الصحيحة
        url = f"{BASE_URL}/passMobile?name={username}&ApiKey={api_key}&pn={phone_number}&pid={project_id}&serial=2"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                return data.get("code") == 200
            else:
                logger.warning(f"passMobile failed: status={response.status_code}, body={response.text}")
                return False
        except Exception as e:
            logger.error(f"Error canceling number {phone_number}: {e}")
            return False

    @staticmethod
    async def add_blacklist(username: str, api_key: str, phone_number: str, project_id: str = "0257") -> bool:
        """إضافة رقم إلى القائمة السوداء - addBlack"""
        url = f"{BASE_URL}/addBlack?name={username}&ApiKey={api_key}&pn={phone_number}&pid={project_id}"
        try:
            response = await _get_with_retry(url)
            if response.status_code == 200:
                data = response.json()
                return data.get("code") == 200
        except Exception as e:
            logger.error(f"Error adding {phone_number} to blacklist: {e}")
        return False

    @staticmethod
    async def get_balance(username: str, api_key: str) -> float:
        """جلب رصيد الحساب الحقيقي من واجهة البرمجة"""
        return await DurianAPI.get_balance_by_name(username, api_key)
