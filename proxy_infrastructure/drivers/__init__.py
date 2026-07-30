"""
__init__.py
===========
مصنع برامج التشغيل (Driver Factory).
يقوم بإنشاء وإرجاع البرنامج المناسب بناءً على اسم المزود.
"""

from .base import BaseProxyDriver
from .static import StaticProxyDriver
from .webshare import WebshareProxyDriver

_DRIVERS = {
    "STATIC": StaticProxyDriver,
    "WEBSHARE": WebshareProxyDriver,
}

def get_driver_instance(provider_name: str) -> BaseProxyDriver:
    """
    إرجاع نسخة من البرنامج التشغيلي (Driver) بناءً على اسم المزود.
    الافتراضي هو StaticProxyDriver في حال عدم معرفة المزود.
    """
    provider_key = str(provider_name).upper()
    driver_class = _DRIVERS.get(provider_key, StaticProxyDriver)
    return driver_class()
