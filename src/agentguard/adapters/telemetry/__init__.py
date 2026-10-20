"""有界后台遥测导出器，不影响策略决策。"""
from .buffered import EventDispatcher

__all__ = ["EventDispatcher"]
