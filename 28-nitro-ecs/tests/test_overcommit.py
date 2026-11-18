from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from overcommit import host_resources, second_host, summarize_placement, target_task_count


class OvercommitTests(unittest.TestCase):
    def test_target_is_two_sandbox_tasks_per_host_vcpu(self):
        self.assertEqual(target_task_count(8), 16)

    def test_invalid_host_size_is_rejected(self):
        for value in (0, -1, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                target_task_count(value)

    def test_placement_reports_realized_density_per_host(self):
        tasks = [
            {'host': {'ec2InstanceId': 'i-a'}},
            {'host': {'ec2InstanceId': 'i-a'}},
            {'host': {'ec2InstanceId': 'i-b'}},
        ]
        hosts = {
            'i-a': {'instance_id': 'i-a', 'instance_type': 'm7g.2xlarge', 'vcpus': 8},
            'i-b': {'instance_id': 'i-b', 'instance_type': 'm7g.2xlarge', 'vcpus': 8},
        }
        placement = summarize_placement(tasks, hosts)
        self.assertEqual(placement[0]['sandbox_tasks_per_vcpu'], 0.25)
        self.assertEqual(placement[1]['sandbox_tasks_per_vcpu'], 0.125)

    def test_second_host_is_detected_relative_to_baseline(self):
        same = [{'host': {'ec2InstanceId': 'i-a'}}, {'host': {'ec2InstanceId': 'i-a'}}]
        self.assertIsNone(second_host(same))
        spill = same + [{'label': 'x', 'host': {'ec2InstanceId': 'i-b'}}]
        self.assertEqual(second_host(spill)['label'], 'x')

    def test_host_resources_compacts_scheduler_view(self):
        snapshot = host_resources({'ec2InstanceId': 'i-a', 'runningTasksCount': 3,
            'registeredResources': [{'name': 'CPU', 'type': 'INTEGER', 'integerValue': 2048},
                {'name': 'PORTS', 'type': 'STRINGSET', 'stringSetValue': ['22', '2375']}],
            'remainingResources': [{'name': 'MEMORY', 'type': 'INTEGER', 'integerValue': 100}]})
        self.assertEqual(snapshot['registered'], {'CPU': 2048, 'PORTS': 2})
        self.assertEqual(snapshot['remaining'], {'MEMORY': 100})
        self.assertEqual(snapshot['running_tasks'], 3)
        self.assertEqual(snapshot['attributes'], {})


if __name__ == '__main__':
    unittest.main()
