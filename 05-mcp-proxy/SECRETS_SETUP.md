# AWS Secrets Manager 配置指南

本文档说明如何在 AWS Secrets Manager 中配置飞书应用的凭证。

## 前提条件

- AWS CLI 已配置
- 具有创建 Secrets Manager 密钥的权限
- 飞书应用的 APP_ID 和 APP_SECRET

## 方法 1: 使用 AWS CLI 创建密钥

### 创建密钥

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

### 更新现有密钥

```bash
aws secretsmanager update-secret \
    --secret-id feishu-mcp-credentials \
    --secret-string '{
        "APP_ID": "your_new_app_id",
        "APP_SECRET": "your_new_app_secret"
    }' \
    --region us-east-1
```

### 获取密钥值（用于验证）

```bash
aws secretsmanager get-secret-value \
    --secret-id feishu-mcp-credentials \
    --region us-east-1 \
    --query SecretString \
    --output text | jq .
```

## 方法 2: 使用 AWS 控制台

1. 打开 AWS Secrets Manager 控制台
2. 选择正确的区域（例如：us-east-1）
3. 点击 "Store a new secret"
4. 选择 "Other type of secret"
5. 在 "Key/value" 标签页中添加：
   - Key: `APP_ID`, Value: `你的APP_ID`
   - Key: `APP_SECRET`, Value: `你的APP_SECRET`
6. 点击 "Next"
7. 输入密钥名称: `feishu-mcp-credentials`
8. 添加描述（可选）: `Feishu/Lark MCP Server Credentials`
9. 点击 "Next" 跳过密钥轮换
10. 审查并创建密钥

## 方法 3: 使用 Python/Boto3 脚本

创建一个脚本 `setup_secret.py`：

```python
import boto3
import json

def create_feishu_secret(app_id, app_secret, region='us-east-1'):
    """创建飞书凭证密钥"""
    client = boto3.client('secretsmanager', region_name=region)

    secret_data = {
        "APP_ID": app_id,
        "APP_SECRET": app_secret
    }

    try:
        response = client.create_secret(
            Name='feishu-mcp-credentials',
            Description='Feishu/Lark MCP Server Credentials',
            SecretString=json.dumps(secret_data)
        )
        print(f"✅ Secret created successfully!")
        print(f"ARN: {response['ARN']}")
        return response
    except client.exceptions.ResourceExistsException:
        print("Secret already exists, updating...")
        response = client.update_secret(
            SecretId='feishu-mcp-credentials',
            SecretString=json.dumps(secret_data)
        )
        print(f"✅ Secret updated successfully!")
        print(f"ARN: {response['ARN']}")
        return response

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("Usage: python setup_secret.py <APP_ID> <APP_SECRET>")
        sys.exit(1)

    app_id = sys.argv[1]
    app_secret = sys.argv[2]

    create_feishu_secret(app_id, app_secret)
```

运行脚本：

```bash
python setup_secret.py "your_app_id" "your_app_secret"
```

## 环境变量配置

### 指定自定义密钥名称

如果使用不同的密钥名称：

```bash
export SECRET_NAME="my-custom-secret-name"
```

### 指定 AWS 区域

```bash
export AWS_REGION="us-west-2"
```

## IAM 权限要求

容器或 Lambda 函数需要以下 IAM 权限：

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret"
            ],
            "Resource": "arn:aws:secretsmanager:us-east-1:*:secret:feishu-mcp-credentials-*"
        }
    ]
}
```

## 在 AgentCore 部署中使用

### 1. 创建密钥（如上所述）

### 2. 配置 IAM 角色

确保 AgentCore 运行时的 IAM 角色具有访问 Secrets Manager 的权限。

### 3. 部署应用

```bash
agentcore launch
```

服务器将自动从 Secrets Manager 读取凭证。

## 密钥格式

Secrets Manager 中的密钥必须是 JSON 格式，包含以下键之一组合：

```json
{
    "APP_ID": "your_app_id",
    "APP_SECRET": "your_app_secret"
}
```

或者（小写）：

```json
{
    "app_id": "your_app_id",
    "app_secret": "your_app_secret"
}
```

或者（驼峰式）：

```json
{
    "appId": "your_app_id",
    "appSecret": "your_app_secret"
}
```

代码会自动尝试所有这些变体。

## 本地开发

对于本地开发，如果无法访问 AWS Secrets Manager，代码会自动回退到环境变量：

```bash
export APP_ID="your_app_id"
export APP_SECRET="your_app_secret"
python src/server.py
```

## 故障排除

### 问题：无法访问密钥

**错误**：`Error retrieving secret from AWS Secrets Manager`

**解决方案**：
1. 确认 IAM 权限正确
2. 检查密钥名称是否正确
3. 验证 AWS 区域配置
4. 检查 AWS CLI 配置：`aws sts get-caller-identity`

### 问题：密钥格式错误

**错误**：`APP_ID or APP_SECRET not found in Secrets Manager`

**解决方案**：
确保密钥是 JSON 格式，并包含正确的键名（`APP_ID`/`APP_SECRET`）。

### 问题：本地测试无法连接 AWS

**解决方案**：
代码会自动回退到环境变量。设置 `APP_ID` 和 `APP_SECRET` 环境变量即可。

## 安全最佳实践

1. ✅ 使用 Secrets Manager 存储敏感凭证
2. ✅ 定期轮换密钥
3. ✅ 使用最小权限原则配置 IAM
4. ✅ 启用 CloudTrail 审计密钥访问
5. ✅ 不要在代码或日志中硬编码凭证
6. ✅ 使用 AWS KMS 加密密钥

## 相关资源

- [AWS Secrets Manager 文档](https://docs.aws.amazon.com/secretsmanager/)
- [Boto3 Secrets Manager API](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager.html)
- [IAM 最佳实践](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html)
