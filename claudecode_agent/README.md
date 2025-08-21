# Claude Code AgentCore Runtime Example

A simple example demonstrating how to run Claude Code in AWS AgentCore runtime

## Setup
### 1. Generate an Bedrock API key

To generate an API key, follow these steps:
- Sign in to the AWS Management Console and open the Amazon Bedrock console
- In the left navigation panel, select API keys
- Choose either Generate short-term API key or Generate long-term API key
- For long-term keys, set your desired expiration time and optionally configure advanced permissions
- Choose Generate and copy your API key

### 2. Prepare .env and fill the API key

Create a .env file in the root directory with the following content:
```
AWS_BEARER_TOKEN_BEDROCK=<bedrock api key>
CLAUDE_CODE_USE_BEDROCK=1
CLAUDE_CODE_MAX_OUTPUT_TOKENS=16000
MAX_THINKING_TOKENS=1024
```

### 3. Run the setup script to create all necessary AWS resources:

```bash
chmod +x pre_setup.sh
./pre_setup.sh
```
You will get `Created AgentCore execution role Arn:` and `Created ECR repository:`, it will be used in **agentcore configures** step.

This script will:
1. Create an IAM execution role for AgentCore
2. Create an ECR repository for the Docker image
3. Create the required IAM roles for Elastic Beanstalk:
   - `aws-elasticbeanstalk-service-role` - Service role for Elastic Beanstalk
   - `aws-elasticbeanstalk-ec2-role` - EC2 instance role for Elastic Beanstalk

### 4. Run agentcore configures
```bash
uv sync
```

#### Replace below <YOUR_IAM_ROLE_ARN> to `Created AgentCore execution role Arn` in the command result. 
```bash
uv run agentcore config --entrypoint claude_code_agent.py -er <YOUR_IAM_ROLE_ARN>
```
- The command will:
- Ask you to enter a ECR repository: You can get it from `Created ECR repository:` in the command result. 
- Generate a Dockerfile and .dockerignore
- Create a .bedrock_agentcore.yaml configuration file

#### Build and Launch to AgentCore Runtime
```bash
uv run agentcore launch
```

## Invoke AgentCore Runtime
- Run below command to test the agentcore, you will find a website url in the result.  
```bash
uv run agentcore invoke '{"model":"us.anthropic.claude-3-7-sonnet-20250219-v1:0", "prompt": "create a interactive learning website to introduce Transformer in AI, targeting middle school students","system":"You are a web application develop. build and delopy the web application to aws elastic beanstalk using MCP tools. You working dir is /app/docs/"}' 
```

- for more details about invoke agentcore runtime, please refer to [Invoke an AgentCore Runtime agent](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html)
