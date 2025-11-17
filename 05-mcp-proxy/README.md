## 飞书MCP server部署到Agentcore runtime 

[飞书MCP 说明](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/mcp_integration/quick-start-guides/quick-integration-with-openapi-mcp)


## 激活虚拟环境 
```bash
uv sync
source .venv/bin/activate
```

## Setup Congito
- Run below scripts to setup a cognito user pool for MCP runtime
```bash
./setup_cognito_s2s.sh us-east-1
```
运行之后，会生成一个`.cognito-s2.env`文件，里面的信息在下面configure中会用到

## 运行如下生成 AgentCore Runtime MCP configure  
```bash
agentcore configure -e server.py -r us-east-1 --protocol MCP
```
⚠️注意：
1. 输入agent名feishu_mcp
2. Select deployment type: 2. Container
3. Skip Memory

## Edit Docker file
运行configure之后，在.bedrock_agentcore/feishu_mcp/下生成一个Dockerfile，在第3行后，加入nodejs依赖
```Dockerfile
# Install system dependencies including Node.js
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs zip \
    && rm -rf /var/lib/apt/lists/*
```

## 部署 MCP 到AgentCore runtime
运行
```bash
agentcore launch
```

## 在 AWS 中创建密钥，替换APP_ID和APP_SECRET为飞书应用的id和secret
```bash
aws secretsmanager create-secret \
    --name feishu-mcp-credentials \
    --description "Feishu/Lark MCP Server Credentials" \
    --secret-string '{
        "APP_ID": "your_app_id_here",
        "APP_SECRET": "your_app_secret_here"
    }' \
    --region us-east-1
```

- 找到.bedrock_agentcore.yaml中execution role Name（⚠️注意：不要保护arn:aws:iam::xxx:role/），增加secret manager 权限
```bash
aws iam put-role-policy \
  --role-name <agentcore_execution_role> \
  --policy-name SecretsManagerAccessPolicy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ],
        "Resource": "arn:aws:secretsmanager:*:*:secret:feishu-mcp-credentials*"
      }
    ]
  }'
```

## test MCP 是否正常
替换`test_agentcore_mcp.py` runtime_arn为部署好的arn，运行 `python test_agentcore_mcp.py`. 
注意控制台会打印出 mcp http 地址，这个地址就是mcp server 调用地址。
“mcp endpoint: https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3xxx%3Aruntime%2Ffeishu_mcp-xxx/invocations?qualifier=DEFAULT”