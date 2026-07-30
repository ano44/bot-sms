"""
evaluator.py
============
إدارة جودة البروكسيات وقاطع الدائرة (Proxy Evaluator & Circuit Breaker).
يتتبع نجاح وفشل الفحوصات ويمنع تكرار استخدام البروكسيات التالفة.
"""

import time
import logging
import database as db

logger = logging.getLogger(__name__)

class ProxyEvaluator:
    def __init__(self):
        # تتبع مؤقت للبروكسيات المحجورة في الذاكرة (Circuit Breaker status)
        # proxy_id -> block_until_timestamp
        self._quarantine = {}
        
        # تتبع مؤقت لعدد الأخطاء المتتالية لكل منفذ في الجلسة الحالية
        # proxy_id -> consecutive_failures_count
        self._consecutive_failures = {}
        
        # سقف الأخطاء المتتالية لتفعيل قاطع الدائرة (3 أخطاء)
        self.FAILURE_THRESHOLD = 3
        
        # مدة الحجر الصحي المؤقت (15 دقيقة = 900 ثانية)
        self.QUARANTINE_DURATION = 900

    def is_healthy(self, proxy_id: int) -> bool:
        """
        التحقق مما إذا كان البروكسي سليماً وليس في الحجر الصحي حالياً.
        """
        now = time.time()
        block_until = self._quarantine.get(proxy_id, 0)
        
        if now < block_until:
            # لا يزال البروكسي في الحجر الصحي
            remaining = block_until - now
            logger.debug(f"[Evaluator] Proxy #{proxy_id} is in quarantine for another {remaining:.1f}s.")
            return False
            
        # إذا انتهت مدة الحجر، نخرجه تلقائياً
        if proxy_id in self._quarantine:
            self._quarantine.pop(proxy_id, None)
            self._consecutive_failures[proxy_id] = 0
            logger.info(f"[Evaluator] Proxy #{proxy_id} quarantine expired. Released back to pool.")
            
        return True

    def report_success(self, proxy_id: int, latency: float = 0.0):
        """
        تسجيل فحص ناجح للبروكسي.
        يقوم بتصفير عداد الأخطاء وتحديث إحصائيات قاعدة البيانات.
        """
        self._consecutive_failures[proxy_id] = 0
        self._quarantine.pop(proxy_id, None)
        
        # تحديث قاعدة البيانات بشكل غير متزامن لتجنب تعطيل الفحص
        try:
            db.update_proxy_stats(proxy_id, is_success=True, latency=latency)
        except Exception as e:
            logger.error(f"[Evaluator] Failed to update db stats for proxy #{proxy_id}: {e}")

    def report_failure(self, proxy_id: int, is_flood: bool = False):
        """
        تسجيل فحص فاشل للبروكسي.
        إذا تم تجاوز عتبة الأخطاء المتتالية، يتم تفعيل قاطع الدائرة وحظر البروكسي مؤقتاً.
        """
        # زيادة عداد الأخطاء المتتالية
        failures = self._consecutive_failures.get(proxy_id, 0) + 1
        self._consecutive_failures[proxy_id] = failures
        
        # تحديث قاعدة البيانات بالخطأ
        try:
            db.update_proxy_stats(proxy_id, is_success=False, is_flood=is_flood)
        except Exception as e:
            logger.error(f"[Evaluator] Failed to update db stats (failure) for proxy #{proxy_id}: {e}")

        # تفعيل Circuit Breaker في حالتين:
        # 1. حدوث حظر مؤقت (FloodWait) ➜ يحظر فوراً
        # 2. تجاوز عتبة الأخطاء المتتالية (3 أخطاء)
        if is_flood or failures >= self.FAILURE_THRESHOLD:
            block_duration = self.QUARANTINE_DURATION
            if is_flood:
                # إذا كان حظراً مؤقتاً، يمكن وضع البروكسي في الحجر لفترة أطول
                block_duration = 1800 # 30 دقيقة
                logger.warning(f"[Evaluator] Circuit Breaker: Proxy #{proxy_id} hit Telegram Flood. Quarantining for 30m.")
            else:
                logger.warning(f"[Evaluator] Circuit Breaker: Proxy #{proxy_id} reached failure threshold ({failures}/{self.FAILURE_THRESHOLD}). Quarantining for 15m.")
                
            self._quarantine[proxy_id] = time.time() + block_duration

    def get_proxy_score(self, proxy: dict) -> float:
        """
        حساب تقييم الجودة الكلي للبروكسي (Score) بناءً على إحصائياته.
        التقييم يقع بين 0.0 و 1.0.
        """
        success = float(proxy.get("success_count", 0))
        failure = float(proxy.get("failure_count", 0))
        total = success + failure
        
        if total == 0:
            return 0.5 # تقييم افتراضي للبروكسي الجديد
            
        success_rate = success / total
        
        # نأخذ Latency في الحسبان (كلما قل البنج زاد التقييم)
        latency = float(proxy.get("avg_latency", 0.0))
        latency_factor = 1.0
        if latency > 0.0:
            # تقليل جودة البنج إذا كان أعلى من 2.0 ثانية
            latency_factor = max(0.1, 1.0 - (latency / 5.0))
            
        return success_rate * latency_factor


proxy_evaluator = ProxyEvaluator()
