#!/usr/bin/env python3
"""Real ECS task startup, DSH first-turn and task-private filesystem tests."""
import argparse
import datetime as dt
import json
import math
from pathlib import Path
import statistics
import time
import uuid

from aws_cli import AWS, write_json

class TaskNotFoundError(RuntimeError):
    pass


class Runner:
    def __init__(self, state_file, out):
        self.state_file=state_file; self.state=json.loads(state_file.read_text())
        self.out=out; self.aws=AWS(self.state['region']); self.probe_index=0
        self.launched_task_arns=[]
    def describe(self, arn):
        response=self.aws.call('ecs','describe-tasks',{'cluster':self.state['cluster'],'tasks':[arn]})
        failures=response.get('failures',[])
        if not response.get('tasks') and len(failures)==1 and failures[0].get('reason')=='MISSING' and failures[0].get('arn')==arn:
            raise TaskNotFoundError('task record is no longer available: '+arn)
        if response.get('failures'): raise RuntimeError('DescribeTasks failure: '+json.dumps(response['failures']))
        return response['tasks'][0]
    def stop(self, arn):
        task=self.describe(arn)
        if task['lastStatus']!='STOPPED' and task.get('desiredStatus')!='STOPPED':
            self.aws.call('ecs','stop-task',{'cluster':self.state['cluster'],'task':arn,'reason':'DSH demo test completed'})
        deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            task=self.describe(arn)
            if task['lastStatus']=='STOPPED': return task
            time.sleep(2)
        raise TimeoutError('task stop deadline; inspect actual task state before continuing')
    def probe(self, ip, route='/health', body=None):
        self.probe_index+=1
        result=self.aws.invoke(self.state['function'],{'ip':ip,'route':route,'body':body},
                               self.out/f'probe-{self.probe_index:04d}.json')
        if not result.get('ok'): raise RuntimeError('probe did not succeed')
        return result
    def launch(self, label, task_definition_arn=None):
        print('Launching '+label,flush=True)
        start=time.perf_counter(); run_id=uuid.uuid4().hex
        response=self.aws.call('ecs','run-task',{'cluster':self.state['cluster'],
            'taskDefinition':task_definition_arn or self.state['task_definition_arn'], 'count':1,'clientToken':run_id,
            'capacityProviderStrategy':[{'capacityProvider':self.state['capacity_provider'],'weight':1}],
            'networkConfiguration':{'awsvpcConfiguration':{'subnets':[self.state['subnet_id']],
                'securityGroups':[self.state['task_sg']],'assignPublicIp':'DISABLED'}},
            'overrides':{'containerOverrides':[{'name':'dsh','environment':[{'name':'BENCH_RUN_ID','value':run_id}]}]}})
        if response.get('failures'): raise RuntimeError('RunTask failure: '+json.dumps(response['failures']))
        task=response['tasks'][0]; arn=task['taskArn']
        self.launched_task_arns.append(arn)
        self.state['task_arns'].append(arn); write_json(self.state_file,self.state)
        write_json(self.out/(label+'-run-task.json'),response)
        deadline=time.monotonic()+900
        while time.monotonic()<deadline:
            task=self.describe(arn)
            if task['lastStatus']=='STOPPED':
                write_json(self.out/(label+'-failed-task.json'),task)
                raise RuntimeError('task stopped before ready: '+task.get('stoppedReason',''))
            if task['lastStatus']=='RUNNING': break
            time.sleep(2)
        else: raise TimeoutError('ECS task launch deadline')
        running_ms=(time.perf_counter()-start)*1000
        mode=task.get('trustedExecutionSummary',{}).get('isolationMode')
        if mode!='ISOLATED_SANDBOX': raise RuntimeError('actual task is not ISOLATED_SANDBOX: '+str(mode))
        addresses=[item['value'] for attachment in task.get('attachments',[]) for item in attachment.get('details',[])
                   if item['name']=='privateIPv4Address']
        if len(addresses)!=1: raise RuntimeError('task ENI address unavailable or ambiguous')
        ready=self.probe(addresses[0])
        ready_ms=(time.perf_counter()-start)*1000
        if ready['response'].get('run_id')!=run_id or ready['response'].get('uid')!=1000:
            raise RuntimeError('task identity or privilege-drop check failed')
        ci=self.aws.call('ecs','describe-container-instances',{'cluster':self.state['cluster'],
            'containerInstances':[task['containerInstanceArn']]})['containerInstances'][0]
        record={'label':label,'arn':arn,'run_id':run_id,'ip':addresses[0],
            'client_submit_to_running_ms':running_ms,'client_submit_to_ready_ms':ready_ms,
            'task':task,'host':ci,'ready':ready['response']}
        write_json(self.out/(label+'-ready.json'),record)
        print(f'{label} ready in {ready_ms/1000:.3f}s on '+ci['ec2InstanceId'],flush=True)
        return record
    def call(self, target, route, body=None):
        result=self.probe(target['ip'],route,body)
        if result['response'].get('run_id')!=target['run_id'] or result['response'].get('boot_id')!=target['ready']['boot_id']:
            raise RuntimeError('request reached another task or guest')
        return result


def lifecycle(task):
    def timestamp(value):
        if isinstance(value,str): return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        return value
    def delta(end,start):
        if task.get(end) is None or task.get(start) is None: return None
        return (timestamp(task[end])-timestamp(task[start]))*1000
    return {'ecs_created_to_started_ms':delta('startedAt','createdAt'),
            'ecs_image_pull_ms':delta('pullStoppedAt','pullStartedAt')}


def stats(values):
    values=sorted(values)
    if not values: return {'n':0}
    def pct(p): return values[max(0,math.ceil(p*len(values))-1)]
    return {'n':len(values),'min':values[0],'max':values[-1],'mean':statistics.fmean(values),
            'p50':pct(.5),'p90':pct(.9),'p99':pct(.99)}


def measure(runner, rounds, document, persist):
    anchor=None
    boots=set()
    try:
        before=runner.aws.call('ecs','list-container-instances',{'cluster':runner.state['cluster']})
        document['capacity_before']=before
        anchor=runner.launch('capacity-anchor')
        document['capacity_anchor']=anchor
        document['capacity_was_zero']=not before.get('containerInstanceArns')
        persist()
        for number in range(1,rounds+1):
            target=None
            try:
                target=runner.launch(f'round-{number:02d}')
                if target['ready']['boot_id'] in boots: raise RuntimeError('new task reused an existing guest')
                boots.add(target['ready']['boot_id'])
                row={'round':number,'startup':{k:target[k] for k in ('client_submit_to_running_ms','client_submit_to_ready_ms')},
                     'lifecycle':lifecycle(target['task']),'task_arn':target['arn'],'boot_id':target['ready']['boot_id'],
                     'ec2_instance_id':target['host']['ec2InstanceId'],'responses':[]}
                document['records'].append(row); persist()
                # Do NOT run help before the first agent turn: that would prewarm DSH.
                for phase in ('cold','warm'):
                    result=runner.call(target,'/invoke',{'mode':'agent','prompt':'Reply with exactly: pong. Do not use tools.'})
                    row['responses'].append({'phase':phase,'probe_http_ms':result['probe_http_ms'],
                                             **result['response']['result']})
                    persist()
                    if not result['response']['result']['ok']:
                        raise RuntimeError('DSH did not complete: '+str(result['response']['result']['error_class']))
                    if result['response']['result']['answer_matches_expected'] is not True:
                        raise AssertionError('DSH answer is not exactly pong')
            finally:
                if target: runner.stop(target['arn'])
    finally:
        if anchor: runner.stop(anchor['arn'])


def summarize(document):
    summary={}
    for metric in ('client_submit_to_running_ms','client_submit_to_ready_ms'):
        summary[metric]=stats([r['startup'][metric] for r in document['records']])
    for metric in ('ecs_created_to_started_ms','ecs_image_pull_ms'):
        summary[metric]=stats([r['lifecycle'][metric] for r in document['records'] if r['lifecycle'][metric] is not None])
    for phase in ('cold','warm'):
        group=[x for row in document['records'] for x in row['responses'] if x['phase']==phase and x['ok']]
        summary[phase]={m:stats([x[m] for x in group if x[m] is not None]) for m in
                        ('first_event_ms','first_text_ms','total_ms','credentials_ms','probe_http_ms')}
        summary[phase]['exact_pong_count']=sum(x['answer_matches_expected'] is True for x in group)
    document['summary']=summary


def isolation(runner, document, persist):
    active=[]; checks=[]
    document['isolation_checks']=checks
    def check(name,condition):
        checks.append({'name':name,'passed':bool(condition)}); persist()
        if not condition: raise AssertionError(name)
    def read(t,scope,file): return runner.call(t,'/fs',{'op':'read','scope':scope,'file':file})['response']
    def write(t,scope,file,text): return runner.call(t,'/fs',{'op':'write','scope':scope,'file':file,'text':text})['response']
    try:
        a=runner.launch('isolation-A'); active.append(a)
        b=runner.launch('isolation-B'); active.append(b)
        document['isolation_tasks']=[a,b]; persist()
        check('same_physical_EC2_instance',a['host']['ec2InstanceId']==b['host']['ec2InstanceId'])
        check('different_guest_boots',a['ready']['boot_id']!=b['ready']['boot_id'])
        for target in (a,b):
            storage=runner.call(target,'/storage')['response']
            document.setdefault('storage_probes',[]).append(storage); persist()
            check(target['label']+'_root_readonly',storage['root_write_error']=='EROFS')
        write(a,'workspace','shared.txt','A-private')
        check('B_cannot_see_A_new_file',read(b,'workspace','shared.txt')['exists'] is False)
        write(b,'workspace','shared.txt','B-private')
        check('same_filename_different_content',read(a,'workspace','shared.txt')['text']=='A-private'
              and read(b,'workspace','shared.txt')['text']=='B-private')
        write(a,'home','private.marker','A-home'); write(b,'home','private.marker','B-home')
        check('DSH_HOME_task_private',read(a,'home','private.marker')['text']=='A-home'
              and read(b,'home','private.marker')['text']=='B-home')
        write(a,'tmp','dsh-isolation.marker','A-tmp')
        check('tmp_task_private',read(b,'tmp','dsh-isolation.marker')['exists'] is False)
        runner.stop(a['arn']); active.remove(a)
        new_a=runner.launch('isolation-A-recreated'); active.append(new_a)
        document['isolation_recreated_task']=new_a; persist()
        check('new_task_new_guest',new_a['ready']['boot_id']!=a['ready']['boot_id'])
        check('ephemeral_workspace_not_persistent',read(new_a,'workspace','shared.txt')['exists'] is False)
        check('ephemeral_DSH_HOME_not_persistent',read(new_a,'home','private.marker')['exists'] is False)
        check('ephemeral_tmp_not_persistent',read(new_a,'tmp','dsh-isolation.marker')['exists'] is False)
        check('B_unaffected',read(b,'workspace','shared.txt')['text']=='B-private')
    finally:
        for task in active: runner.stop(task['arn'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',type=Path,default=Path(__file__).resolve().parent/'state.json')
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--rounds',type=int,default=5)
    p.add_argument('--mode',choices=('all','performance','isolation'),default='all')
    args=p.parse_args()
    if args.rounds<1 or args.rounds>20: p.error('rounds must be 1-20')
    args.out.mkdir(parents=True,exist_ok=False)
    runner=Runner(args.state,args.out)
    document={'tested_at':dt.datetime.now(dt.timezone.utc).isoformat(),'rounds_requested':args.rounds,
              'isolation_mode':'ISOLATED_SANDBOX','records':[],'status':'running'}
    def persist(): write_json(args.out/'results.json',document)
    try:
        runner.aws.invoke(runner.state['function'],{'op':'noop'},args.out/'probe-warmup.json')
        if args.mode in ('all','performance'): measure(runner,args.rounds,document,persist)
        if args.mode in ('all','isolation'): isolation(runner,document,persist)
        document['status']='passed'
    except Exception as error:
        document['status']='failed'; document['error']=str(error)
        raise
    finally:
        # Include tasks whose launch failed AFTER RunTask acknowledgement.
        cleanup=[]
        for arn in runner.launched_task_arns:
            try:
                if runner.describe(arn)['lastStatus']!='STOPPED': runner.stop(arn)
            except Exception as error: cleanup.append({'task':arn,'error':type(error).__name__})
        document['cleanup_errors']=cleanup; document['rounds_executed']=len(document['records'])
        summarize(document)
        if cleanup: document['status']='failed'
        persist()
        print('Evidence: '+str(args.out/'results.json'),flush=True)
    print(json.dumps(document.get('summary',{}),indent=2))
    if document.get('cleanup_errors'): raise RuntimeError('demo task cleanup incomplete; inspect results')


if __name__ == '__main__':
    main()
