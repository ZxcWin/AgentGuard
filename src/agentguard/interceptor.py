"""应用拦截器的向后兼容导入。"""

from .application.interceptors import BaseInterceptor, InterceptorChain

__all__ = ["BaseInterceptor", "InterceptorChain"]
