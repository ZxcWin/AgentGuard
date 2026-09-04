"""Backward-compatible imports for application interceptors."""

from .application.interceptors import BaseInterceptor, InterceptorChain

__all__ = ["BaseInterceptor", "InterceptorChain"]
