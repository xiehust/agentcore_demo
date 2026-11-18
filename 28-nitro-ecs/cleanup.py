#!/usr/bin/env python3
"""Stop only this demo's tasks and release its Managed Instances capacity provider."""
import argparse
import json
from pathlib import Path
import time

from aws_cli import AWS, write_json
from benchmark import Runner, TaskNotFoundError

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--state',type=Path,default=Path(__file__).resolve().parent/'state.json')
args=parser.parse_args()
state=json.loads(args.state.read_text())
out=Path(__file__).resolve().parent/'results/cleanup'
out.mkdir(parents=True,exist_ok=True)
runner=Runner(args.state,out)
expired_records=[]
for arn in state.get('task_arns',[]):
    try:
        runner.stop(arn)
    except TaskNotFoundError:
        expired_records.append(arn)
        print('Task record unavailable; cluster emptiness must still be verified: '+arn,flush=True)
aws=AWS(state['region'],out)
cluster=aws.call('ecs','describe-clusters',{'clusters':[state['cluster']]})['clusters'][0]
if cluster.get('runningTasksCount') or cluster.get('pendingTasksCount'):
    raise RuntimeError('cluster still has tasks; refuse capacity cleanup')
# Managed Instances are AWS-managed: do not stop/terminate their EC2s directly.
aws.call('ecs','put-cluster-capacity-providers',{'cluster':state['cluster'],
    'capacityProviders':[],'defaultCapacityProviderStrategy':[]})
for name in state.get('capacity_providers_created',[state['capacity_provider']]):
    providers=aws.call('ecs','describe-capacity-providers',{'capacityProviders':[name]}).get('capacityProviders',[])
    if not providers or providers[0].get('status')=='INACTIVE': continue
    if providers[0].get('updateStatus')!='DELETE_IN_PROGRESS':
        aws.call('ecs','delete-capacity-provider',{'capacityProvider':name})
    deadline=time.monotonic()+900
    while time.monotonic()<deadline:
        result=aws.call('ecs','describe-capacity-providers',{'capacityProviders':[name]})
        providers=result.get('capacityProviders',[])
        if not providers or providers[0].get('status')=='INACTIVE': break
        if providers[0].get('updateStatus')=='DELETE_FAILED': raise RuntimeError('capacity deletion failed')
        time.sleep(5)
    else: raise TimeoutError('capacity still deleting; inspect before any replay')
functions=aws.call('lambda','list-functions').get('Functions',[])
if any(function['FunctionName']==state['function'] for function in functions):
    aws.call('lambda','delete-function',{'FunctionName':state['function']})
state['status']='tasks_stopped_capacity_released_probe_deleted'
state['task_records_unavailable']=expired_records
state['retained_resources']=['ECR image','CloudWatch logs','IAM roles/profile','security groups','empty ECS cluster']
write_json(args.state,state)
print(json.dumps({'status':state['status'],'retained':state['retained_resources']},indent=2))
