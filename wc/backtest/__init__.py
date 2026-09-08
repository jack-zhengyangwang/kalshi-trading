"""Backtesting: replay a strategy spec over historical candlesticks.

Market-agnostic by construction — the engine keys off ticker, close time, and
price, never off sport or series semantics. See docs/backtester/.
"""
