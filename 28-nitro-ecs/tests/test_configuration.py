from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from configuration import capacity_provider, task_definition, efs_client_policy, efs_filesystem_policy
from aws_cli import AWS, write_json
import json
import tempfile
from unittest.mock import patch, Mock

class ConfigurationTests(unittest.TestCase):
    def state(self):
        return {k:'test' for k in ('capacity_provider','cluster','infrastructure_role_arn','instance_profile_arn',
            'subnet_id','host_sg','family','task_role_arn','execution_role_arn','image_uri','region','bench_token','log_group')}
    def test_isolation_is_managed_instances_not_fargate(self):
        task=task_definition(self.state())
        self.assertEqual(task['requiresCompatibilities'],['MANAGED_INSTANCES'])
        self.assertEqual(task['trustedExecutionConfiguration']['isolationMode'],'ISOLATED_SANDBOX')
        self.assertEqual(task['networkMode'],'awsvpc')
        self.assertNotIn('ephemeralStorage',task)
        self.assertTrue(task['containerDefinitions'][0]['readonlyRootFilesystem'])
    def test_cpu_unset_task_definition_for_oversubscription(self):
        task=task_definition(self.state(),family='cpu-oversub',cpu=None,memory=512)
        self.assertNotIn('cpu',task)
        self.assertEqual(task['memory'],'512')
        self.assertEqual(task['trustedExecutionConfiguration']['isolationMode'],'ISOLATED_SANDBOX')
    def test_private_volumes_do_not_export_a_host_path(self):
        for volume in task_definition(self.state())['volumes']:
            self.assertEqual(volume['host'],{})
            self.assertNotIn('configuredAtLaunch',volume)
    def test_capacity_provider_enables_nitro(self):
        template=capacity_provider(self.state())['managedInstancesProvider']['instanceLaunchTemplate']
        self.assertTrue(template['nitroEnclavesEnabled'])
        self.assertNotIn('localStorageConfiguration',template)
    def test_capacity_provider_pins_one_instance_shape(self):
        requirements=capacity_provider(self.state(),'m7g.large')['managedInstancesProvider'][
            'instanceLaunchTemplate']['instanceRequirements']
        self.assertEqual(requirements['allowedInstanceTypes'],['m7g.large'])
        self.assertEqual(requirements['vCpuCount'],{'min':2,'max':2})
        self.assertEqual(requirements['memoryMiB'],{'min':8192,'max':8192})
        with self.assertRaises(ValueError):
            capacity_provider(self.state(),'t4g.large')
    def test_efs_is_tls_and_iam_scoped_per_access_point(self):
        task=task_definition(self.state(),efs={'filesystem_id':'fs-test','access_point_id':'fsap-test'})
        volume=task['volumes'][0]['efsVolumeConfiguration']
        self.assertEqual(volume['authorizationConfig'],{'accessPointId':'fsap-test','iam':'ENABLED'})
        self.assertEqual(volume['transitEncryption'],'ENABLED')
        statement=efs_client_policy('fs-arn','ap-arn')['Statement'][0]
        self.assertEqual(statement['Condition']['StringEquals']['elasticfilesystem:AccessPointArn'],'ap-arn')
        self.assertNotIn('elasticfilesystem:ClientRootAccess',statement['Action'])
    def test_failed_mutation_is_not_replayed(self):
        result=Mock(returncode=1,stderr='AccessDenied',stdout='')
        with patch('subprocess.run',return_value=result) as run,self.assertRaises(RuntimeError):
            AWS('region').call('ecs','run-task',{})
        run.assert_called_once()
    def test_filesystem_policy_closes_other_identity_path_tls_and_root_access(self):
        policy=efs_filesystem_policy('fs-arn','ap-arn','role-arn')
        deny={item['Sid']:item for item in policy['Statement'] if item['Effect']=='Deny'}
        self.assertEqual(deny['DenyOtherIdentities']['Condition']['ArnNotEquals']['aws:PrincipalArn'],'role-arn')
        self.assertEqual(deny['DenyOtherPaths']['Condition']['StringNotEquals']['elasticfilesystem:AccessPointArn'],'ap-arn')
        self.assertEqual(deny['DenyRoot']['Action'],'elasticfilesystem:ClientRootAccess')
        self.assertEqual(deny['DenyPlaintext']['Condition']['Bool']['aws:SecureTransport'],'false')
    def test_state_write_is_private_and_replaces_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'state.json'
            write_json(target,{'status':'before'})
            write_json(target,{'status':'after'})
            self.assertEqual(json.loads(target.read_text()),{'status':'after'})
            self.assertEqual(target.stat().st_mode & 0o777,0o600)
            self.assertEqual(list(target.parent.iterdir()),[target])

if __name__=='__main__': unittest.main()
