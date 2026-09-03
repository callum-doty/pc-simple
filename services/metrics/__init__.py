"""
Metrics layer for the admin dashboard.

Split out of services/dashboard_service.py during the dashboard rebuild. The
old service computed 13 metric groups, each inventing its own idea of "the
corpus" — two cards labelled *total* disagreed, and the same concept read 100
in one panel and thousands in another.

Three rules hold that from recurring:

  1. Every metric returns a ``Metric`` envelope (envelope.py), never a bare
     number, so a value cannot reach a template without the population it was
     computed over.
  2. Every population comes from ``scope.py``. No module here filters on
     ``Document.status`` directly.
  3. Every clock reading comes from ``scope.now_utc()``. The document
     timestamps are ``timestamptz``; naive ``datetime.utcnow()`` must not
     return.

See the rebuild spec for the metric contracts each zone implements.
"""

from services.metrics.envelope import Metric, MetricGroup
from services.metrics import scope

__all__ = ["Metric", "MetricGroup", "scope"]
