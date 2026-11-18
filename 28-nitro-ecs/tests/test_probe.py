import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('probe_handler',
    Path(__file__).resolve().parents[1]/'app/probe_handler.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.context = Mock()
        self.context.get_remaining_time_in_millis.return_value = 180000

    def test_rejects_outside_vpc_and_ipv6(self):
        with patch.dict(os.environ, {'ALLOWED_CIDR': '10.0.0.0/16'}):
            for address in ('127.0.0.1', '169.254.169.254', '8.8.8.8', '::1'):
                with self.subTest(address=address), self.assertRaises(ValueError):
                    probe.handler({'ip': address}, self.context)

    def test_post_is_not_replayed(self):
        with patch.dict(os.environ, {'ALLOWED_CIDR': '10.0.0.0/16'}), \
                patch.object(probe.HTTP, 'open', side_effect=OSError('ambiguous timeout')) as request:
            with self.assertRaises(OSError):
                probe.handler({'ip': '10.0.0.10', 'route': '/invoke', 'body': {'mode': 'agent'}}, self.context)
            request.assert_called_once()

    def test_cpu_probe_route_is_allowed(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"operations_per_second":123}'
        with patch.dict(os.environ, {'ALLOWED_CIDR': '10.0.0.0/16'}), \
                patch.object(probe.HTTP, 'open', return_value=response) as request:
            result = probe.handler({'ip': '10.0.0.10', 'route': '/cpu', 'body': {'duration_ms': 1000}}, self.context)
            self.assertEqual(result['response']['operations_per_second'], 123)
            request.assert_called_once()

    def test_health_retries_connect_failure(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"run_id":"demo"}'
        with patch.dict(os.environ, {'ALLOWED_CIDR': '10.0.0.0/16'}), \
                patch.object(probe.HTTP, 'open', side_effect=[OSError('starting'), response]) as request, \
                patch.object(probe.time, 'sleep'):
            result = probe.handler({'ip': '10.0.0.10'}, self.context)
            self.assertEqual(result['response']['run_id'], 'demo')
            self.assertEqual(request.call_count, 2)
