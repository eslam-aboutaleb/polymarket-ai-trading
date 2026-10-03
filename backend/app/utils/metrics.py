"""In-process metrics registry rendered by the /metrics endpoint.

Counters, gauges and histograms live in process memory. They are
intentionally per-worker: the shipped deployment runs a single worker,
so the values are complete; under multiple workers each instance
reports its own view. Other modules record metrics via the helpers
below; the admin-only /metrics endpoint renders Prometheus text format.
"""

import threading


class Counter:
    """Monotonically increasing counter."""

    def __init__(self, name: str, documentation: str = "") -> None:
        self.name = name
        self.documentation = documentation
        self._value = 0.0

    def inc(self, amount: float = 1.0) -> None:
        with _lock:
            self._value += amount

    @property
    def value(self) -> float:
        return self._value


class Gauge:
    """Value that can go up and down."""

    def __init__(self, name: str, documentation: str = "") -> None:
        self.name = name
        self.documentation = documentation
        self._value = 0.0

    def set(self, value: float) -> None:
        with _lock:
            self._value = value

    @property
    def value(self) -> float:
        return self._value


class Histogram:
    """Observation distribution over fixed buckets."""

    BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self, name: str, documentation: str = "") -> None:
        self.name = name
        self.documentation = documentation
        self._sum = 0.0
        self._count = 0
        self._buckets = [0] * len(self.BUCKETS)

    def observe(self, value: float) -> None:
        with _lock:
            self._sum += value
            self._count += 1
            for i, bound in enumerate(self.BUCKETS):
                if value <= bound:
                    self._buckets[i] += 1


_registry: dict[str, Counter | Gauge | Histogram] = {}
_lock = threading.Lock()


def _get_or_create(name: str, documentation: str, cls: type):
    with _lock:
        metric = _registry.get(name)
        if metric is None:
            metric = cls(name, documentation)
            _registry[name] = metric
        return metric


def counter(name: str, documentation: str = "") -> Counter:
    """Get or create a counter."""
    return _get_or_create(name, documentation, Counter)


def gauge(name: str, documentation: str = "") -> Gauge:
    """Get or create a gauge."""
    return _get_or_create(name, documentation, Gauge)


def histogram(name: str, documentation: str = "") -> Histogram:
    """Get or create a histogram."""
    return _get_or_create(name, documentation, Histogram)


def render_prometheus() -> str:
    """Render all registered metrics in Prometheus text format."""
    lines: list[str] = []
    with _lock:
        for name, metric in sorted(_registry.items()):
            lines.append(f"# HELP {name} {metric.documentation}".rstrip())
            lines.append(f"# TYPE {name} {type(metric).__name__.lower()}")
            if isinstance(metric, Histogram):
                # Histogram._buckets already stores cumulative
                # counts (observe() increments every bucket whose
                # bound is >= the value, standard Prometheus
                # semantics). Emit them directly — accumulating
                # here a second time would double-count and can
                # make finite buckets exceed _count/+Inf.
                for bound, count in zip(metric.BUCKETS, metric._buckets, strict=True):
                    lines.append(f'{name}_bucket{{le="{bound}"}} {count}')
                lines.append(f'{name}_bucket{{le="+Inf"}} {metric._count}')
                lines.append(f"{name}_sum {metric._sum}")
                lines.append(f"{name}_count {metric._count}")
            else:
                lines.append(f"{name} {metric.value}")
    return "\n".join(lines) + "\n"
