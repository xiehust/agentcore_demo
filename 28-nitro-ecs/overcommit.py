#!/usr/bin/env python3
"""Measure a 1:2 target CPU-oversubscription workload on ECS Nitro Sandboxes."""
import argparse
from collections import Counter
import concurrent.futures
import datetime as dt
import json
import math
from pathlib import Path
import statistics
import time

from aws_cli import write_json
from benchmark import Runner, stats
from configuration import task_definition

RATIO = 2
TASK_MEMORY_MIB = 512
CPU_PROBE_SECONDS = 10
MAX_TASKS = 32


def target_task_count(host_vcpus, tasks_per_vcpu=RATIO):
    if not isinstance(host_vcpus, int) or host_vcpus < 1:
        raise ValueError('host_vcpus must be a positive integer')
    if not isinstance(tasks_per_vcpu, int) or tasks_per_vcpu < 1:
        raise ValueError('tasks_per_vcpu must be a positive integer')
    return host_vcpus * tasks_per_vcpu


def describe_host(aws, instance_id):
    response = aws.call('ec2', 'describe-instances', {'InstanceIds': [instance_id]})
    reservations = response.get('Reservations', [])
    instances = [instance for reservation in reservations for instance in reservation.get('Instances', [])]
    if len(instances) != 1:
        raise RuntimeError('could not resolve exactly one EC2 instance for ' + instance_id)
    instance_type = instances[0]['InstanceType']
    type_response = aws.call('ec2', 'describe-instance-types', {'InstanceTypes': [instance_type]})
    types = type_response.get('InstanceTypes', [])
    if len(types) != 1:
        raise RuntimeError('could not resolve instance type details for ' + instance_type)
    vcpu_info = types[0]['VCpuInfo']
    return {'instance_id': instance_id, 'instance_type': instance_type,
        'vcpus': vcpu_info['DefaultVCpus'], 'cores': vcpu_info.get('DefaultCores'),
        'threads_per_core': vcpu_info.get('DefaultThreadsPerCore')}


def run_cpu_probe(runner, task, duration_seconds, output):
    result = runner.aws.invoke(runner.state['function'], {
        'ip': task['ip'], 'route': '/cpu',
        'body': {'duration_ms': duration_seconds * 1000}}, output)
    if not result.get('ok'):
        raise RuntimeError('CPU probe failed for ' + task['label'])
    response = result.get('response', {})
    if response.get('run_id') != task['run_id'] or response.get('boot_id') != task['ready']['boot_id']:
        raise RuntimeError('CPU probe reached a different task or guest for ' + task['label'])
    rate = response.get('operations_per_second')
    if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
        raise RuntimeError('CPU probe returned an invalid throughput for ' + task['label'])
    return {'label': task['label'], 'instance_id': task['host']['ec2InstanceId'],
        'operations_per_second': rate, 'cpu_probe_ms': response['cpu_probe_ms'],
        'operations': response['operations'], 'checksum': response['checksum']}


def host_resources(container_instance):
    """Keep ECS's registered/remaining scheduler resources for one container instance."""
    def compact(resources):
        rows = {}
        for item in resources or []:
            value = item.get('integerValue')
            if item.get('type') == 'STRINGSET':
                value = len(item.get('stringSetValue', []))
            elif item.get('type') in ('DOUBLE', 'LONG'):
                value = item.get(item['type'].lower() + 'Value')
            rows[item['name']] = value
        return rows
    return {'instance_id': container_instance['ec2InstanceId'],
        'running_tasks': container_instance.get('runningTasksCount'),
        'pending_tasks': container_instance.get('pendingTasksCount'),
        'registered': compact(container_instance.get('registeredResources')),
        'remaining': compact(container_instance.get('remainingResources')),
        'attributes': {item['name']: item.get('value') for item in container_instance.get('attributes', [])
            if not item['name'].startswith('com.amazonaws.ecs.capability')}}


def host_network(aws, instance_id):
    """Network interfaces attached to the host; awsvpc task ENIs normally attach here."""
    response = aws.call('ec2', 'describe-network-interfaces', {'Filters': [
        {'Name': 'attachment.instance-id', 'Values': [instance_id]}]})
    rows = [{'type': item.get('InterfaceType'), 'device_index': item.get('Attachment', {}).get('DeviceIndex'),
        'network_card_index': item.get('Attachment', {}).get('NetworkCardIndex'),
        'requester_managed': item.get('RequesterManaged'), 'description': item.get('Description', '')[:80]}
        for item in response.get('NetworkInterfaces', [])]
    return {'instance_id': instance_id, 'attached_enis': len(rows),
        'by_type': dict(Counter(row['type'] for row in rows)), 'interfaces': rows}


def task_enis(aws, task):
    """Describe the task's own ENI while it is running (it is deleted when the task stops)."""
    ids = [item['value'] for attachment in task.get('attachments', [])
        for item in attachment.get('details', []) if item['name'] == 'networkInterfaceId']
    if not ids:
        return []
    response = aws.call('ec2', 'describe-network-interfaces', {'NetworkInterfaceIds': ids})
    return [{'id': item['NetworkInterfaceId'], 'type': item.get('InterfaceType'),
        'status': item.get('Status'), 'requester_managed': item.get('RequesterManaged'),
        'attached_instance_id': item.get('Attachment', {}).get('InstanceId'),
        'attached_instance_owner': item.get('Attachment', {}).get('InstanceOwnerId'),
        'device_index': item.get('Attachment', {}).get('DeviceIndex'),
        'network_card_index': item.get('Attachment', {}).get('NetworkCardIndex'),
        'description': item.get('Description', '')[:80]} for item in response.get('NetworkInterfaces', [])]


def second_host(tasks):
    """Return the first task whose host differs from the baseline task's host."""
    first = tasks[0]['host']['ec2InstanceId']
    return next((task for task in tasks[1:] if task['host']['ec2InstanceId'] != first), None)


def summarize_placement(tasks, host_specs):
    counts = Counter(task['host']['ec2InstanceId'] for task in tasks)
    rows = []
    for instance_id, count in sorted(counts.items()):
        spec = host_specs[instance_id]
        rows.append({**spec, 'sandbox_tasks': count,
            'sandbox_tasks_per_vcpu': count / spec['vcpus']})
    return rows


def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    runner = Runner(args.state, args.out)
    document = {'tested_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'isolation_mode': 'ISOLATED_SANDBOX', 'target_host_vcpu_to_sandbox_tasks': f'1:{args.tasks_per_vcpu}',
        'task_level_cpu': 'unset', 'task_memory_mib': TASK_MEMORY_MIB,
        'capacity_provider': None, 'single_host_required': args.single_host,
        'host_resource_snapshots': [],
        'cpu_probe_seconds': args.duration_seconds, 'status': 'running',
        'tasks': [], 'cpu_samples': [], 'cleanup_errors': []}
    task_definition_arn = None
    failure = None

    def persist():
        write_json(args.out / 'results.json', document)

    try:
        document['capacity_provider'] = runner.state['capacity_provider']
        runner.aws.invoke(runner.state['function'], {'op': 'noop'}, args.out / 'probe-warmup.json')
        definition = task_definition(runner.state,
            family=runner.state['family'] + '-cpu-oversub', cpu=None, memory=TASK_MEMORY_MIB)
        response = runner.aws.call('ecs', 'register-task-definition', definition)
        task_definition_arn = response['taskDefinition']['taskDefinitionArn']
        document['task_definition_arn'] = task_definition_arn
        document['task_definition_cpu_omitted'] = 'cpu' not in definition
        document['task_count_is_target_not_guaranteed_ratio'] = True
        persist()

        def record_launch(task):
            document['tasks'].append({'label': task['label'], 'arn': task['arn'],
                'instance_id': task['host']['ec2InstanceId'], 'boot_id': task['ready']['boot_id'],
                'client_submit_to_ready_ms': task['client_submit_to_ready_ms']})
            document['host_resource_snapshots'].append({'after': task['label'],
                **host_resources(task['host']),
                'network': host_network(runner.aws, task['host']['ec2InstanceId']),
                'task_enis': task_enis(runner.aws, task['task'])})
            persist()

        baseline = runner.launch('cpu-baseline', task_definition_arn=task_definition_arn)
        tasks = [baseline]
        record_launch(baseline)
        host_specs = {baseline['host']['ec2InstanceId']:
            describe_host(runner.aws, baseline['host']['ec2InstanceId'])}
        baseline_spec = host_specs[baseline['host']['ec2InstanceId']]
        target_count = target_task_count(baseline_spec['vcpus'], args.tasks_per_vcpu)
        if target_count > MAX_TASKS:
            raise ValueError(f'target is {target_count} tasks; limit is {MAX_TASKS}')
        document['baseline_host'] = baseline_spec
        document['target_sandbox_tasks'] = target_count
        document['baseline_cpu'] = run_cpu_probe(runner, baseline, args.duration_seconds,
            args.out / 'cpu-baseline.json')
        persist()

        for number in range(2, target_count + 1):
            task = runner.launch(f'cpu-target-{number:02d}', task_definition_arn=task_definition_arn)
            tasks.append(task)
            record_launch(task)
            if args.single_host and second_host(tasks):
                document['placement'] = summarize_placement(tasks[:-1], host_specs)
                document['single_host_capacity_observed'] = len(tasks) - 1
                raise RuntimeError(f"task {number} was placed on a second host after "
                    f"{len(tasks) - 1} sandboxes on {baseline_spec['instance_id']}")
            if task['host']['ec2InstanceId'] not in host_specs:
                host_specs[task['host']['ec2InstanceId']] = describe_host(
                    runner.aws, task['host']['ec2InstanceId'])
            persist()

        document['placement'] = summarize_placement(tasks, host_specs)
        document['host_resources_before_cpu_load'] = host_resources(runner.aws.call('ecs',
            'describe-container-instances', {'cluster': runner.state['cluster'],
            'containerInstances': [baseline['task']['containerInstanceArn']]})['containerInstances'][0])
        persist()
        document['concurrent_probe_started_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tasks), MAX_TASKS)) as pool:
            futures = [pool.submit(run_cpu_probe, runner, task, args.duration_seconds,
                args.out / f"cpu-{task['label']}.json") for task in tasks]
            for future in concurrent.futures.as_completed(futures):
                document['cpu_samples'].append(future.result())
                persist()

        document['concurrent_probe_finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        baseline_rate = document['baseline_cpu']['operations_per_second']
        concurrent_rates = [sample['operations_per_second'] for sample in document['cpu_samples']]
        placement = document['placement']
        document['summary'] = {
            'target_tasks_per_vcpu': args.tasks_per_vcpu,
            'max_observed_tasks_per_vcpu': max(row['sandbox_tasks_per_vcpu'] for row in placement),
            'target_density_observed': any(row['sandbox_tasks_per_vcpu'] >= args.tasks_per_vcpu for row in placement),
            'baseline_operations_per_second': baseline_rate,
            'concurrent_task_operations_per_second': stats(concurrent_rates),
            'mean_task_rate_vs_single_task': statistics.fmean(concurrent_rates) / baseline_rate,
            'aggregate_rate_in_baseline_task_equivalents': sum(concurrent_rates) / baseline_rate,
            'host_count': len(placement)}
        observed = document['summary']['target_density_observed']
        if args.single_host:
            observed = observed and len(placement) == 1
        document['status'] = 'passed' if observed else 'target_not_observed'
    except Exception as error:
        failure = error
        document['status'] = 'failed'
        document['error'] = str(error)
    finally:
        for arn in runner.launched_task_arns:
            try:
                if runner.describe(arn)['lastStatus'] != 'STOPPED':
                    runner.stop(arn)
            except Exception as error:
                document['cleanup_errors'].append({'task': arn, 'error': type(error).__name__})
        if task_definition_arn and not document['cleanup_errors']:
            try:
                runner.aws.call('ecs', 'deregister-task-definition', {'taskDefinition': task_definition_arn})
                document['task_definition_deregistered'] = True
            except Exception as error:
                document['cleanup_errors'].append({'task_definition': task_definition_arn,
                    'error': type(error).__name__})
        if document['cleanup_errors']:
            document['status'] = 'failed'
        persist()
        print('Evidence: ' + str(args.out / 'results.json'), flush=True)

    if failure:
        raise failure
    if document['status'] != 'passed':
        raise RuntimeError('1:2 target density was not observed; inspect results.json')
    print(json.dumps(document['summary'], indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path(__file__).resolve().parent / 'state.json')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=int, default=CPU_PROBE_SECONDS)
    parser.add_argument('--tasks-per-vcpu', type=int, default=RATIO,
        help='target sandboxes per host vCPU (1-5); use >2 to probe the per-host cap')
    parser.add_argument('--allow-multiple-hosts', dest='single_host', action='store_false',
        help='keep launching after a task lands on a second host (default: fail fast)')
    args = parser.parse_args()
    if not 1 <= args.tasks_per_vcpu <= 5:
        parser.error('--tasks-per-vcpu must be 1-5')
    if not 1 <= args.duration_seconds <= 30:
        parser.error('--duration-seconds must be 1-30')
    run(args)


if __name__ == '__main__':
    main()
