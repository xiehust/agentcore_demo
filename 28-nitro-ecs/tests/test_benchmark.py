import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark import Runner, TaskNotFoundError, lifecycle, stats, measure, summarize


class BenchmarkTests(unittest.TestCase):
    def test_cleanup_tracks_only_current_acknowledged_tasks_even_when_launch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / 'state.json'
            state_file.write_text(json.dumps({'region': 'us-east-1', 'cluster': 'test',
                'task_arns': ['old-task'], 'task_definition_arn': 'definition',
                'capacity_provider': 'provider', 'subnet_id': 'subnet', 'task_sg': 'sg'}))
            runner = Runner(state_file, root)
            self.assertEqual(runner.launched_task_arns, [])
            runner.aws = Mock()
            runner.aws.call.side_effect = [{'tasks': [{'taskArn': 'new-task'}]},
                {'tasks': [{'taskArn': 'new-task', 'lastStatus': 'RUNNING'}]}]
            with self.assertRaisesRegex(RuntimeError, 'not ISOLATED_SANDBOX'):
                runner.launch('failed')
            self.assertEqual(runner.launched_task_arns, ['new-task'])
            self.assertEqual(json.loads(state_file.read_text())['task_arns'], ['old-task', 'new-task'])

    def test_missing_task_record_is_distinct_from_other_describe_failures(self):
        runner = Runner.__new__(Runner)
        runner.state = {'cluster': 'test'}
        runner.aws = Mock()
        runner.aws.call.return_value = {'tasks': [], 'failures': [{'arn': 'task', 'reason': 'MISSING'}]}
        with self.assertRaises(TaskNotFoundError):
            runner.describe('task')

    def test_other_describe_failures_are_not_skippable_as_expired_records(self):
        runner = Runner.__new__(Runner)
        runner.state = {'cluster': 'test'}
        runner.aws = Mock()
        for failure in ({'arn': 'task', 'reason': 'ACCESS_DENIED'}, {'arn': 'other', 'reason': 'MISSING'}):
            runner.aws.call.return_value = {'tasks': [], 'failures': [failure]}
            with self.assertRaises(RuntimeError) as raised:
                runner.describe('task')
            self.assertNotIsInstance(raised.exception, TaskNotFoundError)

    def test_cli_iso_timestamps(self):
        result = lifecycle({'createdAt': '2026-11-17T14:00:00+00:00',
            'startedAt': '2026-11-17T14:00:02.500Z',
            'pullStartedAt': '2026-11-17T14:00:01+00:00',
            'pullStoppedAt': '2026-11-17T14:00:02+00:00'})
        self.assertEqual(result, {'ecs_created_to_started_ms': 2500, 'ecs_image_pull_ms': 1000})

    def test_numeric_and_missing_timestamps(self):
        self.assertEqual(lifecycle({'createdAt': 1, 'startedAt': 2}),
            {'ecs_created_to_started_ms': 1000, 'ecs_image_pull_ms': None})

    def test_nearest_rank_and_empty_stats(self):
        self.assertEqual(stats([]), {'n': 0})
        self.assertEqual(stats([5, 1, 3, 2, 4])['p90'], 5)

    def test_wrong_answer_fails_and_stops_both_tasks(self):
        runner = Mock()
        runner.state = {'cluster': 'test'}
        runner.aws.call.return_value = {'containerInstanceArns': []}
        target = {'arn': 'target', 'ready': {'boot_id': 'boot'}, 'task': {},
            'host': {'ec2InstanceId': 'host'}, 'client_submit_to_running_ms': 1,
            'client_submit_to_ready_ms': 2}
        runner.launch.side_effect = [{'arn': 'anchor'}, target]
        runner.call.return_value = {'probe_http_ms': 3,
            'response': {'result': {'ok': True, 'answer_matches_expected': False}}}
        with self.assertRaisesRegex(AssertionError, 'exactly pong'):
            measure(runner, 1, {'records': []}, Mock())
        self.assertEqual([entry.args[0] for entry in runner.stop.call_args_list], ['target', 'anchor'])

    def test_partial_results_keep_failed_run_and_exact_counts(self):
        document = {'status': 'failed', 'records': [{'startup': {
            'client_submit_to_running_ms': 1000, 'client_submit_to_ready_ms': 2000},
            'lifecycle': {'ecs_created_to_started_ms': None, 'ecs_image_pull_ms': None},
            'responses': [{'phase': 'cold', 'ok': True, 'first_event_ms': 1,
                'first_text_ms': 2, 'total_ms': 3, 'credentials_ms': 4,
                'probe_http_ms': 5, 'answer_matches_expected': False}]}]}
        summarize(document)
        self.assertEqual(document['status'], 'failed')
        self.assertEqual(document['summary']['cold']['first_text_ms']['n'], 1)
        self.assertEqual(document['summary']['cold']['exact_pong_count'], 0)
        self.assertEqual(document['summary']['warm']['first_text_ms']['n'], 0)

    def test_stop_waits_without_replaying_an_accepted_stop(self):
        runner = Runner.__new__(Runner)
        runner.state = {'cluster': 'test'}
        runner.aws = Mock()
        runner.describe = Mock(side_effect=[
            {'lastStatus': 'RUNNING', 'desiredStatus': 'STOPPED'},
            {'lastStatus': 'STOPPING', 'desiredStatus': 'STOPPED'},
            {'lastStatus': 'STOPPED', 'desiredStatus': 'STOPPED'}])
        with patch('benchmark.time.sleep'):
            self.assertEqual(runner.stop('task')['lastStatus'], 'STOPPED')
        runner.aws.call.assert_not_called()

    def test_stop_requests_once_then_waits_for_actual_stopped_state(self):
        runner = Runner.__new__(Runner)
        runner.state = {'cluster': 'test'}
        runner.aws = Mock()
        runner.describe = Mock(side_effect=[
            {'lastStatus': 'RUNNING', 'desiredStatus': 'RUNNING'},
            {'lastStatus': 'STOPPING', 'desiredStatus': 'STOPPED'},
            {'lastStatus': 'STOPPED', 'desiredStatus': 'STOPPED'}])
        with patch('benchmark.time.sleep'):
            runner.stop('task')
        runner.aws.call.assert_called_once_with('ecs', 'stop-task', {
            'cluster': 'test', 'task': 'task', 'reason': 'DSH demo test completed'})
