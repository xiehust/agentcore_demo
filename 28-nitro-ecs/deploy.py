#!/usr/bin/env python3
"""Create a dedicated ECS Nitro Sandbox demo using existing private/NAT networking."""
import argparse
import json
from pathlib import Path
import time
import zipfile

from aws_cli import AWS, write_json
from configuration import capacity_provider, task_definition

ROOT = Path(__file__).resolve().parent

def deploy(args):
    if args.state.exists():
        raise ValueError('state already exists; inspect it, do not recreate resources')
    aws = AWS(args.region)
    who = aws.call('sts', 'get-caller-identity')
    account = who['Account']
    subnet = aws.call('ec2', 'describe-subnets', {'SubnetIds': [args.subnet]})['Subnets'][0]
    vpc = aws.call('ec2', 'describe-vpcs', {'VpcIds': [subnet['VpcId']]})['Vpcs'][0]
    state = {'account': account, 'region': args.region, 'prefix': args.name, 'cluster': args.name,
        'capacity_provider': args.name + '-mi', 'family': args.name, 'repository': args.name,
        'function': args.name + '-probe', 'log_group': '/ecs/' + args.name,
        'vpc_id': vpc['VpcId'], 'vpc_cidr': vpc['CidrBlock'], 'subnet_id': args.subnet,
        'roles': [], 'task_arns': [], 'status': 'deploying'}
    def save(): write_json(args.state, state)
    save()
    def role(name, principal, managed=None, policy=None):
        trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
            'Principal': {'Service': principal}, 'Action': 'sts:AssumeRole'}]}
        if principal == 'ecs-tasks.amazonaws.com':
            trust['Statement'][0]['Condition'] = {'StringEquals': {'aws:SourceAccount': account},
                'ArnLike': {'aws:SourceArn': f'arn:aws:ecs:{args.region}:{account}:*'}}
        result = aws.call('iam', 'create-role', {'RoleName': name, 'AssumeRolePolicyDocument': json.dumps(trust),
            'Tags': [{'Key': 'Project', 'Value': 'dsh-nitro-ecs'}]})['Role']
        state['roles'].append({'name': name, 'managed': managed, 'inline': bool(policy)})
        save()
        if managed: aws.call('iam', 'attach-role-policy', {'RoleName': name, 'PolicyArn': managed})
        if policy: aws.call('iam', 'put-role-policy', {'RoleName': name, 'PolicyName': 'Demo', 'PolicyDocument': json.dumps(policy)})
        return result['Arn']
    instance_name = 'ecsInstanceRole-' + args.name
    instance_arn = role(instance_name, 'ec2.amazonaws.com',
        'arn:aws:iam::aws:policy/AmazonECSInstanceRolePolicyForManagedInstances')
    profile = aws.call('iam', 'create-instance-profile', {'InstanceProfileName': instance_name})['InstanceProfile']
    state['instance_profile_name'], state['instance_profile_arn'] = instance_name, profile['Arn']; save()
    aws.call('iam', 'add-role-to-instance-profile', {'InstanceProfileName': instance_name, 'RoleName': instance_name})
    state['infrastructure_role_arn'] = role(args.name + '-infra', 'ecs.amazonaws.com',
        'arn:aws:iam::aws:policy/AmazonECSInfrastructureRolePolicyForManagedInstances'); save()
    model_policy = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
        'Action': ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        'Resource': f'arn:aws:bedrock:{args.region}::foundation-model/deepseek.v3.2'}]}
    state['task_role_arn'] = role(args.name + '-task', 'ecs-tasks.amazonaws.com', policy=model_policy); save()
    exec_policy = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': 'ecr:GetAuthorizationToken', 'Resource': '*'},
        {'Effect': 'Allow', 'Action': ['ecr:BatchGetImage','ecr:GetDownloadUrlForLayer','ecr:BatchCheckLayerAvailability'],
         'Resource': f'arn:aws:ecr:{args.region}:{account}:repository/{args.name}'},
        {'Effect': 'Allow', 'Action': ['logs:CreateLogStream','logs:PutLogEvents'],
         'Resource': f'arn:aws:logs:{args.region}:{account}:log-group:{state["log_group"]}:*'}]}
    state['execution_role_arn'] = role(args.name + '-exec', 'ecs-tasks.amazonaws.com', policy=exec_policy); save()
    state['probe_role_arn'] = role(args.name + '-probe', 'lambda.amazonaws.com',
        'arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole'); save()
    for key in ('host_sg','task_sg','probe_sg'):
        group = aws.call('ec2','create-security-group', {'GroupName': args.name + '-' + key,
            'Description': 'Dedicated ECS Nitro DSH demo', 'VpcId': state['vpc_id']})
        state[key] = group['GroupId']; save()
    aws.call('ec2','authorize-security-group-ingress', {'GroupId': state['task_sg'], 'IpPermissions': [
        {'IpProtocol':'tcp','FromPort':8080,'ToPort':8080,'UserIdGroupPairs':[{'GroupId':state['probe_sg']}]}]})
    aws.call('logs','create-log-group', {'logGroupName':state['log_group']})
    aws.call('logs','put-retention-policy', {'logGroupName':state['log_group'],'retentionInDays':7})
    aws.call('ecr','create-repository', {'repositoryName':args.name,'imageTagMutability':'IMMUTABLE',
        'encryptionConfiguration':{'encryptionType':'AES256'},'imageScanningConfiguration':{'scanOnPush':True}})
    state['image_tag_uri'] = f'{account}.dkr.ecr.{args.region}.amazonaws.com/{args.name}:dsh-0.2.2-rc.1'
    aws.call('ecs','create-cluster', {'clusterName':state['cluster']}); save()
    aws.call('ecs','create-capacity-provider', capacity_provider(state)); save()
    finish(args,state,aws)


def finish(args,state,aws):
    def save(): write_json(args.state,state)
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        provider=aws.call('ecs','describe-capacity-providers',{'capacityProviders':[state['capacity_provider']]})['capacityProviders'][0]
        if provider['status']=='ACTIVE': break
        if provider.get('updateStatus') in ('CREATE_FAILED','UPDATE_FAILED'):
            raise RuntimeError('capacity provider failed: '+provider.get('updateStatusReason',''))
        time.sleep(3)
    else: raise TimeoutError('capacity provider activation deadline')
    aws.call('ecs','put-cluster-capacity-providers', {'cluster':state['cluster'],
        'capacityProviders':[state['capacity_provider']],
        'defaultCapacityProviderStrategy':[{'capacityProvider':state['capacity_provider'],'weight':1}]})
    package = ROOT / 'probe.zip'
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / 'app/probe_handler.py', 'probe_handler.py')
    # IAM propagation is observed with GetRole; Lambda creation is submitted once.
    aws.call('iam','get-role', {'RoleName':args.name+'-probe'})
    import subprocess, os
    command = ['aws','--region',args.region,'lambda','create-function','--function-name',state['function'],
        '--runtime','python3.13','--handler','probe_handler.handler','--role',state['probe_role_arn'],
        '--zip-file','fileb://'+str(package),'--timeout','180','--memory-size','256',
        '--vpc-config',json.dumps({'SubnetIds':[state['subnet_id']],'SecurityGroupIds':[state['probe_sg']]}),
        '--environment',json.dumps({'Variables':{'ALLOWED_CIDR':state['vpc_cidr']}})]
    result=subprocess.run(command,capture_output=True,text=True,timeout=120,
        env={**os.environ,'AWS_PAGER':'','AWS_MAX_ATTEMPTS':'1'})
    if result.returncode: raise RuntimeError(result.stderr.strip())
    state['probe_function_arn']=json.loads(result.stdout)['FunctionArn']; save()
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        info=aws.call('lambda','get-function-configuration',{'FunctionName':state['function']})
        if info['State']=='Active': break
        if info['State']=='Failed': raise RuntimeError('probe Lambda failed: '+info.get('StateReason',''))
        time.sleep(3)
    else: raise TimeoutError('probe Lambda activation deadline')
    state['status']='infrastructure_created_image_pending'; save()
    print(json.dumps({k:state[k] for k in ('cluster','capacity_provider','image_tag_uri','function','status')},indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('create','finish','register'))
    parser.add_argument('--region',default='us-east-1')
    parser.add_argument('--name',default='dsh-nitro-ecs-20261117')
    parser.add_argument('--subnet')
    parser.add_argument('--image-tag')
    parser.add_argument('--state',type=Path,default=ROOT/'state.json')
    args=parser.parse_args()
    if args.action=='create':
        if not args.subnet: parser.error('create requires a private subnet with NAT egress')
        deploy(args)
    elif args.action=='finish':
        state=json.loads(args.state.read_text()); aws=AWS(state['region'])
        finish(args,state,aws)
    else:
        state=json.loads(args.state.read_text()); aws=AWS(state['region'])
        tag=args.image_tag or state['image_tag_uri'].rsplit(':',1)[1]
        image=aws.call('ecr','describe-images', {'repositoryName':state['repository'],
            'imageIds':[{'imageTag':tag}]})['imageDetails'][0]
        state['image_tag_uri']=state['image_tag_uri'].rsplit(':',1)[0]+':'+tag
        state['image_uri']=state['image_tag_uri'].split(':')[0]+'@'+image['imageDigest']
        state['image_size_bytes']=image['imageSizeInBytes']
        definition=task_definition(state)
        result=aws.call('ecs','register-task-definition',definition)['taskDefinition']
        state['task_definition_arn']=result['taskDefinitionArn']; state['status']='ready'
        write_json(args.state,state)
        write_json(ROOT/'generated/task-definition.json',definition)
        write_json(ROOT/'generated/capacity-provider.json',capacity_provider(state))
        print('Registered '+state['task_definition_arn'])


if __name__ == '__main__':
    main()
