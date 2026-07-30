"""
__init__.py
===========
تصدير مدير البروكسي كطبقة موحدة جاهزة للاستخدام من بقية مكوّنات البوت.
"""

from .manager import proxy_manager
from .tasks.health_checker import check_all_proxies_health
