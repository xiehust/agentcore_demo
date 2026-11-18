#!/usr/bin/env python3
"""Replace idle demo capacity in another AZ, preserving Nitro explicitly."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aws_cli import AWS,write_json
from configuration import capacity_provider
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--state',type=Path,default=ROOT/'state.json')
parser.add_argument('--subnet',required=True)
parser.add_argument('--capacity-provider',required=True)
args=parser.parse_args()
state_file=args.state
state=json.loads(state_file.read_text()); aws=AWS(state['region'])
subnet=args.subnet
info=aws.call('ec2','describe-subnets',{'SubnetIds':[subnet]})['Subnets'][0]
if info['VpcId']!=state['vpc_id'] or info['MapPublicIpOnLaunch']:
    raise ValueError('choose a private subnet in the demo VPC')
cluster=aws.call('ecs','describe-clusters',{'clusters':[state['cluster']]})['clusters'][0]
if cluster['runningTasksCount'] or cluster['pendingTasksCount']:
    raise RuntimeError('stop demo tasks before moving capacity')
previous=state['capacity_provider']
if args.capacity_provider==previous:
    raise ValueError('replacement must have a new capacity provider name')
replacement={**state,'capacity_provider':args.capacity_provider,'subnet_id':subnet}
aws.call('ecs','create-capacity-provider',capacity_provider(replacement))
state.setdefault('capacity_providers_created',[previous]).append(args.capacity_provider)
write_json(state_file,state)
deadline=time.monotonic()+600
while time.monotonic()<deadline:
    cp=aws.call('ecs','describe-capacity-providers',{'capacityProviders':[args.capacity_provider]})['capacityProviders'][0]
    if cp.get('status')=='ACTIVE': break
    if cp.get('updateStatus')=='CREATE_FAILED': raise RuntimeError(cp.get('updateStatusReason','create failed'))
    time.sleep(3)
else: raise TimeoutError('capacity create deadline; inspect before replay')
template=cp['managedInstancesProvider']['instanceLaunchTemplate']
if not template['nitroEnclavesEnabled'] or template['networkConfiguration']['subnets']!=[subnet]:
    raise RuntimeError('replacement configuration differs from the request')
aws.call('ecs','put-cluster-capacity-providers',{'cluster':state['cluster'],
 'capacityProviders':[args.capacity_provider],
 'defaultCapacityProviderStrategy':[{'capacityProvider':args.capacity_provider,'weight':1}]})
state['previous_subnet_id']=state['subnet_id']; state['subnet_id']=subnet
state['previous_capacity_provider']=previous; state['capacity_provider']=args.capacity_provider
write_json(state_file,state)
aws.call('ecs','delete-capacity-provider',{'capacityProvider':previous})
write_json(ROOT/'results/capacity-AZ-replacement.json',cp)
write_json(ROOT/'generated/capacity-provider.json',capacity_provider(state))
print('Replaced demo capacity in '+info['AvailabilityZone']+'; Nitro verified enabled')
