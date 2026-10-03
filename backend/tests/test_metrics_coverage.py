"""Coverage for ``app.utils.metrics`` — the in-process Prometheus registry.

Exercises every metric type (Counter, Gauge, Histogram), the
get-or-create registry helpers, and the Prometheus text rendering
including cumulative histogram buckets, the +Inf bucket, sum and
count lines, sorted output and the empty-registry edge case.
"""

import threading
import unittest

import app.utils.metrics as metrics_module
from app.utils.metrics import (
    Counter,
    Gauge,
    Histogram,
    _get_or_create,
    counter,
    gauge,
    histogram,
    render_prometheus,
)


class MetricsTestCase(unittest.TestCase):
    """Isolate the module-level registry per test."""

    def setUp(self):
        self._saved_registry = dict(metrics_module._registry)
        metrics_module._registry.clear()
        self.addCleanup(self._restore_registry)

    def _restore_registry(self):
        metrics_module._registry.clear()
        metrics_module._registry.update(self._saved_registry)


class CounterTests(MetricsTestCase):
    def test_initial_value_is_zero(self):
        c = Counter("http_requests", "Total requests")
        self.assertEqual(c.value, 0.0)
        self.assertEqual(c.name, "http_requests")
        self.assertEqual(c.documentation, "Total requests")

    def test_inc_defaults_to_one(self):
        c = Counter("http_requests")
        c.inc()
        self.assertEqual(c.value, 1.0)

    def test_inc_with_custom_amount(self):
        c = Counter("http_requests")
        c.inc(2.5)
        c.inc(0.5)
        self.assertEqual(c.value, 3.0)


class GaugeTests(MetricsTestCase):
    def test_initial_value_is_zero(self):
        g = Gauge("queue_depth", "Pending jobs")
        self.assertEqual(g.value, 0.0)
        self.assertEqual(g.name, "queue_depth")
        self.assertEqual(g.documentation, "Pending jobs")

    def test_set_updates_value(self):
        g = Gauge("queue_depth")
        g.set(7.5)
        self.assertEqual(g.value, 7.5)
        g.set(-3.0)
        self.assertEqual(g.value, -3.0)


class HistogramTests(MetricsTestCase):
    def test_initial_state(self):
        h = Histogram("req_latency", "Request latency")
        self.assertEqual(h._sum, 0.0)
        self.assertEqual(h._count, 0)
        self.assertEqual(h._buckets, [0] * len(Histogram.BUCKETS))

    def test_observe_updates_sum_and_count(self):
        h = Histogram("req_latency")
        h.observe(0.5)
        h.observe(1.5)
        self.assertEqual(h._sum, 2.0)
        self.assertEqual(h._count, 2)

    def test_observe_increments_all_buckets_at_or_above_value(self):
        h = Histogram("req_latency")
        h.observe(0.003)
        # 0.003 <= every bound, so every bucket counts the observation.
        self.assertEqual(h._buckets, [1] * len(Histogram.BUCKETS))

    def test_observe_only_increments_larger_buckets(self):
        h = Histogram("req_latency")
        h.observe(7.0)
        expected = [0] * (len(Histogram.BUCKETS) - 1) + [1]
        self.assertEqual(h._buckets, expected)

    def test_observe_accumulates_across_values(self):
        h = Histogram("req_latency")
        h.observe(0.003)
        h.observe(7.0)
        expected = [1] * (len(Histogram.BUCKETS) - 1) + [2]
        self.assertEqual(h._buckets, expected)
        self.assertEqual(h._sum, 7.003)
        self.assertEqual(h._count, 2)


class RegistryTests(MetricsTestCase):
    def test_get_or_create_creates_and_reuses(self):
        first = _get_or_create("metric_a", "docs", Counter)
        second = _get_or_create("metric_a", "other docs", Counter)
        self.assertIs(first, second)
        self.assertEqual(first.documentation, "docs")
        self.assertIs(metrics_module._registry["metric_a"], first)

    def test_counter_factory(self):
        c = counter("factory_counter", "created via factory")
        self.assertIsInstance(c, Counter)
        self.assertIs(counter("factory_counter"), c)

    def test_gauge_factory(self):
        g = gauge("factory_gauge", "created via factory")
        self.assertIsInstance(g, Gauge)
        self.assertIs(gauge("factory_gauge"), g)

    def test_histogram_factory(self):
        h = histogram("factory_histogram", "created via factory")
        self.assertIsInstance(h, Histogram)
        self.assertIs(histogram("factory_histogram"), h)

    def test_registry_returns_existing_instance_regardless_of_type(self):
        c = counter("shared_name")
        g = gauge("shared_name")
        self.assertIs(c, g)


class RenderPrometheusTests(MetricsTestCase):
    def test_empty_registry_renders_blank_line(self):
        self.assertEqual(render_prometheus(), "\n")

    def test_counter_rendering(self):
        c = counter("http_requests", "Total HTTP requests")
        c.inc()
        c.inc(2.0)
        output = render_prometheus()
        self.assertIn("# HELP http_requests Total HTTP requests\n", output)
        self.assertIn("# TYPE http_requests counter\n", output)
        self.assertIn("http_requests 3.0\n", output)

    def test_gauge_rendering(self):
        g = gauge("queue_depth", "Pending jobs")
        g.set(4.0)
        output = render_prometheus()
        self.assertIn("# TYPE queue_depth gauge\n", output)
        self.assertIn("queue_depth 4.0\n", output)

    def test_help_line_is_rstripped_without_documentation(self):
        c = counter("bare")
        c.inc()
        output = render_prometheus()
        self.assertIn("# HELP bare\n", output)

    def test_histogram_rendering(self):
        h = histogram("req_latency", "Request latency")
        h.observe(0.003)
        h.observe(7.0)
        output = render_prometheus()
        self.assertIn("# TYPE req_latency histogram\n", output)
        # Buckets are rendered exactly as observe() maintains
        # them: cumulative, without being summed a second time.
        self.assertIn('req_latency_bucket{le="0.005"} 1\n', output)
        self.assertIn('req_latency_bucket{le="0.01"} 1\n', output)
        self.assertIn('req_latency_bucket{le="10.0"} 2\n', output)
        self.assertIn('req_latency_bucket{le="+Inf"} 2\n', output)
        self.assertIn("req_latency_sum 7.003\n", output)
        self.assertIn("req_latency_count 2\n", output)

    def test_histogram_buckets_are_cumulative(self):
        h = histogram("req_latency")
        h.observe(0.003)  # lands in the 0.005 bucket
        h.observe(0.02)  # lands in the 0.025 bucket
        output = render_prometheus()
        # Rendered buckets are cumulative: 1, 1, 2, 2, ...
        self.assertIn('req_latency_bucket{le="0.005"} 1\n', output)
        self.assertIn('req_latency_bucket{le="0.01"} 1\n', output)
        self.assertIn('req_latency_bucket{le="0.025"} 2\n', output)
        self.assertIn('req_latency_bucket{le="+Inf"} 2\n', output)

    def test_output_is_sorted_by_name(self):
        counter("zeta")
        counter("alpha")
        gauge("mid")
        output = render_prometheus()
        self.assertLess(output.index("alpha"), output.index("mid"))
        self.assertLess(output.index("mid"), output.index("zeta"))

    def test_concurrent_increments_are_not_lost(self):
        c = counter("concurrent")

        def _worker():
            for _ in range(1000):
                c.inc()

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(c.value, 4000.0)


if __name__ == "__main__":
    unittest.main()
