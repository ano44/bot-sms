"""
base.py
=======
الواجهة الموحدة (Interface) لجميع موفري البروكسيات.
ترث جميع برامج التشغيل (Drivers) من هذه الفئة لضمان ثبات منطق الاستدعاء.
"""

from abc import ABC, abstractmethod

class BaseProxyDriver(ABC):
    
    @abstractmethod
    async def get_proxy(self, country_code: str) -> dict | None:
        """
        جلب بيانات البروكسي المتوافقة للدولة المحددة.
        يجب أن تُرجع قاموساً بالصيغة التالية أو None:
        {
            "host": "123.45.67.89",
            "port": 1080,
            "username": "user",
            "password": "pass",
            "proxy_type": "SOCKS5",
            "rotation_url": "http://..."  # اختياري
        }
        """
        pass

    @abstractmethod
    async def rotate_ip(self, rotation_url: str) -> bool:
        """
        طلب تدوير أو تغيير الـ IP للمنفذ عبر الـ API الخاص بالمزود.
        تُرجع True في حال النجاح و False في حال الفشل.
        """
        pass

    @abstractmethod
    async def check_health(self, proxy_config: dict) -> bool:
        """
        فحص سلامة البروكسي بشكل مباشر للتأكد من قدرته على الاتصال.
        """
        pass
