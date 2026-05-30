"""
Funnel logic — re-exports from metrics.py to keep the suggested layout.
The implementation lives in metrics.py because funnel and conversion
share the POS-correlation primitive.
"""
from .metrics import store_funnel  # noqa: F401
