## Setup VPC and VPC endpoints
### VPC
1. 创建VPC和Subnets 
```bash
./setup-vpc.sh
```
这个脚本将创建资源：
```text
1个VPC（10.0.0.0/16）

1个互联网网关

4个子网（2个公有子网，2个私有子网，跨2个可用区）

2个弹性IP

2个NAT网关

3个路由表（1个公有，2个私有）

1个安全组（Bedrock AgentCore runtime专用）

1个VPC Endpoint节点（Bedrock Runtime）
```

2. 创建VPC Endpoint和Gateway
```bash
./setup-vpc-endpoints.sh
```
以上脚本将创建资源：
```
ECR Docker端点 - 用于拉取Docker镜像

ECR API端点 - 用于ECR API调用

S3 Gateway端点 - 用于访问S3存储服务

CloudWatch Logs端点 - 用于日志记录

Bedrock端点 - 用于访问AWS Bedrock运行时服务
```
## 创建IAM execution role
1. 运行脚本创建role
```bash
./create_vpc_strands_agent_role.sh
```
这个role将包含BedrockAgentCoreFullAccess, AmazonBedrockFullAccess，ECR等权限用于后续创建runtime

## Create ECR repository and deploy
### Deploying to ECR
1. Create an ECR repository:
```bash
aws ecr create-repository --repository-name vpc-strands-agent --region us-west-2
```
2. Log in to ECR:

```bash
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-west-2.amazonaws.com
```
3. Build and push to ECR:

```bash
docker buildx build --platform linux/arm64 -t <account-id>.dkr.ecr.us-west-2.amazonaws.com/vpc-strands-agent:latest --push .
```
4. Verify the image was pushed:
```bash
aws ecr describe-images --repository-name vpc-strands-agent --region us-west-2
```


## 部署agent runtime
修改`deploy_agent.py`代码中的 subnets为前面脚本创建的2个私有子网，安全组为前面创建的安全组。 
```python 
subnets = ['subnet-xxx', 'subnet-xxx']
sgs = ['sg-xxx']
```
运行`python deploy_agent.py`


## 测试 agent runtime
修改`invoke_agent.py`代码中的 agentRuntimeArn 为前面deploy的实际arn。   
运行`python deploy_agent.py`


## 清理VPC资源
```bash
./cleanup_vpc.sh
```