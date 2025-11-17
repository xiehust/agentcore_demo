from fastmcp import FastMCP, Client
from fastmcp.client.transports import StdioTransport
from starlette.responses import JSONResponse
import os
import logging
import json
import boto3
from botocore.exceptions import ClientError
from fastmcp.server.proxy import FastMCPProxy
# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_secret():
    """从 AWS Secrets Manager 获取密钥"""
    secret_name = os.getenv("SECRET_NAME", "feishu-mcp-credentials")
    region_name = os.getenv("AWS_REGION", "us-east-1")

    # 创建 Secrets Manager 客户端
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )

    try:
        logger.info(f"Attempting to retrieve secret: {secret_name} from region: {region_name}")
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
        logger.info("Successfully retrieved secret from AWS Secrets Manager")
    except ClientError as e:
        logger.error(f"Error retrieving secret from AWS Secrets Manager: {e}")
        # 如果是本地开发环境，回退到环境变量
        logger.warning("Falling back to environment variables")
        return {
            "APP_ID": os.getenv("APP_ID", ""),
            "APP_SECRET": os.getenv("APP_SECRET", "")
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving secret: {e}")
        return {
            "APP_ID": os.getenv("APP_ID", ""),
            "APP_SECRET": os.getenv("APP_SECRET", "")
        }

    # 解析密钥
    secret = get_secret_value_response['SecretString']
    return json.loads(secret)


# 获取密钥
try:
    credentials = get_secret()
    app_id = credentials.get("APP_ID") 
    app_secret = credentials.get("APP_SECRET") 

    if app_id:
        logger.info(f"Successfully loaded credentials. APP_ID: {app_id[:4]}****")
    else:
        logger.error("No APP_ID found in Secrets Manager or environment variables")
except Exception as e:
    logger.error(f"Error loading credentials: {e}")
    # 回退到环境变量

# 验证凭据
if not app_id or not app_secret:
    logger.error("❌ APP_ID and APP_SECRET are required but not found!")
    logger.error("Please set them in AWS Secrets Manager or as environment variables")
    raise ValueError("Missing required credentials: APP_ID and APP_SECRET")

# 创建 StdioTransport
transport = StdioTransport(
    command="npx",
    args=[
        "-y",
        "@larksuiteoapi/lark-mcp",
        "mcp",
        "--token-mode",
        "tenant_access_token",
    ],
    env={
        "APP_ID": app_id,
        "APP_SECRET": app_secret,
        "LARK_DOMAIN": os.getenv("LARK_DOMAIN", "https://open.feishu.cn"),
    }
)

# 创建 Client
client = Client(transport)

# 创建代理服务器
mcp = FastMCP.as_proxy(
    client,
    name="Lark MCP Streamable HTTP Server"
)

if __name__ == "__main__":
    logger.info("Starting Lark MCP Streamable HTTP Server...")

    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        stateless_http=True
    )
