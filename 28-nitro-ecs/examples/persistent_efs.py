#!/usr/bin/env python3
"""Generate tenant-specific EFS task definitions/policies. Does not create storage."""
import argparse
import json
import re
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from configuration import task_definition, efs_client_policy, efs_filesystem_policy
from aws_cli import write_json

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--state',type=Path,required=True)
parser.add_argument('--tenant',required=True)
parser.add_argument('--filesystem-id',required=True)
parser.add_argument('--access-point-id',required=True)
parser.add_argument('--task-role-arn',required=True)
parser.add_argument('--out',type=Path,required=True)
args=parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}',args.tenant):
    parser.error('tenant must be a safe ECS family and single directory name')
state=json.loads(args.state.read_text())
if args.out.exists(): raise ValueError('output already exists')
args.out.mkdir(parents=True)
fs=f'arn:aws:elasticfilesystem:{state["region"]}:{state["account"]}:file-system/{args.filesystem_id}'
ap=f'arn:aws:elasticfilesystem:{state["region"]}:{state["account"]}:access-point/{args.access_point_id}'
definition=task_definition(state,family=state['family']+'-'+args.tenant,
    efs={'filesystem_id':args.filesystem_id,'access_point_id':args.access_point_id},task_role=args.task_role_arn)
write_json(args.out/'task-definition.json',definition)
write_json(args.out/'efs-client-policy.json',efs_client_policy(fs,ap))
write_json(args.out/'efs-filesystem-policy.json',efs_filesystem_policy(fs,ap,args.task_role_arn))
write_json(args.out/'access-point-settings.json',{'PosixUser':{'Uid':1000,'Gid':1000},
    'RootDirectory':{'Path':'/tenants/'+args.tenant,'CreationInfo':{'OwnerUid':1000,'OwnerGid':1000,'Permissions':'0700'}}})
print('Generated isolated ECS/EFS configuration for '+args.tenant)
