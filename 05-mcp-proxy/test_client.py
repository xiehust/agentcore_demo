"""
MCP Client 测试脚本
用于测试 Lark MCP Proxy Server
"""
import asyncio
from fastmcp import Client
import json
import sys
from mcp.client.streamable_http import streamablehttp_client

mcpURL = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A434444145045%3Aruntime%2Ffeishu_mcp-vZoq8mDAuS/invocations?qualifier=DEFAULT"
token= "eyJraWQiOiJQYmgxWk1XeUJRWTJLSURrVEhCNXQ4RkE2Q1pWQzN4NVJ6WWNUYzFOcW84PSIsImFsZyI6IlJTMjU2In0.eyJzdWIiOiIybmRjaXVmbzMydDA3cDlhNDUwN2wzbGtlbiIsInRva2VuX3VzZSI6ImFjY2VzcyIsInNjb3BlIjoibXktYXBpXC93cml0ZSBteS1hcGlcL3JlYWQiLCJhdXRoX3RpbWUiOjE3NjMzNTYwMjYsImlzcyI6Imh0dHBzOlwvXC9jb2duaXRvLWlkcC51cy1lYXN0LTEuYW1hem9uYXdzLmNvbVwvdXMtZWFzdC0xX01UVnJ6YkJNWCIsImV4cCI6MTc2MzM1OTYyNiwiaWF0IjoxNzYzMzU2MDI2LCJ2ZXJzaW9uIjoyLCJqdGkiOiI2NTcyOTJlOS1kNTc4LTRlM2YtODRmNi01ODYzMzU0YWRhYzEiLCJjbGllbnRfaWQiOiIybmRjaXVmbzMydDA3cDlhNDUwN2wzbGtlbiJ9.C-cHEOVFoqfPNEv5XyQHxmUMOt-xx01YVXiWWBZHAvc6s21XB2wCdTqGjc26DBsJHn2QpARU9pUAr27pwDXHfAme8p0n7WFKx8peGU-5Egvm0G8KYQyTXPYXGg8BsAWKVsHq4orEcHCz1h584RRSsfDkaoKAo_fM3PbbSpr2Z3207d4v8Gpr0c51Vox9sJ3zkEkYE9soOBIGwE8pGMTneem_ayxlYCN6nXb4wN-K26w2kbt0cdMhtopwtLat75POWJaroRQNDUHOQi4WsemDKg2BPB8a72Bi-u3PG6F6L-WBmVBC8_gxGopzpV4yYhf4X9ZexoLKCNRmxk_L5uYVyw"


config = {
    "mcpServers": {
        "server_name": {
            # Remote HTTP/SSE server
            "transport": "http",  # or "sse" 
            "url": mcpURL,
            "headers": {"Authorization": f"Bearer {token}"},
        }
    }
}
  
    
async def test_mcp_server():
    """测试 MCP 服务器的各项功能"""

    # 创建 Client，直接使用 URL
    client = Client("http://127.0.0.1:8000/mcp")    
    # client = Client(config)

    try:
        print("=" * 60)
        print("连接到 MCP 服务器...")
        print("=" * 60)

        async with client:
            # 获取服务器信息
            print("\n[1] 获取服务器信息")
            print("-" * 60)
            if client.initialize_result:
                info = client.initialize_result.serverInfo
                print(f"服务器名称: {info.name}")
                print(f"服务器版本: {info.version}")
                print(f"协议版本: {client.initialize_result.protocolVersion}")

            # 列出所有工具
            print("\n[2] 列出所有可用工具")
            print("-" * 60)
            tools_result = await client.list_tools()
            # tools_result 可能是列表或包含 tools 属性的对象
            tools = tools_result if isinstance(tools_result, list) else (tools_result.tools if hasattr(tools_result, 'tools') else [])
            if tools:
                print(f"找到 {len(tools)} 个工具:\n")
                for tool in tools:
                    print(f"  📦 {tool.name}")
                    print(f"     描述: {tool.description or '无描述'}")
                    if hasattr(tool, 'inputSchema') and tool.inputSchema:
                        schema = tool.inputSchema
                        if 'properties' in schema:
                            print(f"     参数: {', '.join(schema['properties'].keys())}")
                    print()
            else:
                print("没有找到可用的工具")

            # 列出所有资源
            print("\n[3] 列出所有可用资源")
            print("-" * 60)
            resources_result = await client.list_resources()
            resources = resources_result if isinstance(resources_result, list) else (resources_result.resources if hasattr(resources_result, 'resources') else [])
            if resources:
                print(f"找到 {len(resources)} 个资源:\n")
                for resource in resources:
                    print(f"  📄 {resource.name}")
                    print(f"     URI: {resource.uri}")
                    print(f"     描述: {resource.description or '无描述'}")
                    if hasattr(resource, 'mimeType'):
                        print(f"     类型: {resource.mimeType}")
                    print()
            else:
                print("没有找到可用的资源")

            # 列出所有提示词模板
            print("\n[4] 列出所有提示词模板")
            print("-" * 60)
            prompts_result = await client.list_prompts()
            prompts = prompts_result if isinstance(prompts_result, list) else (prompts_result.prompts if hasattr(prompts_result, 'prompts') else [])
            if prompts:
                print(f"找到 {len(prompts)} 个提示词模板:\n")
                for prompt in prompts:
                    print(f"  💬 {prompt.name}")
                    print(f"     描述: {prompt.description or '无描述'}")
                    if hasattr(prompt, 'arguments'):
                        print(f"     参数: {prompt.arguments}")
                    print()
            else:
                print("没有找到提示词模板")

            # 如果有工具，尝试调用第一个工具（带错误处理）
            if tools:
                print("\n[5] 测试调用工具")
                print("-" * 60)
                first_tool = tools[0]
                print(f"尝试调用工具: {first_tool.name}")

                # 根据工具的 schema 构造测试参数
                test_args = {}
                if hasattr(first_tool, 'inputSchema') and first_tool.inputSchema:
                    schema = first_tool.inputSchema
                    if 'properties' in schema:
                        for prop_name, prop_schema in schema['properties'].items():
                            prop_type = prop_schema.get('type', 'string')
                            # 提供默认测试值
                            if prop_type == 'string':
                                test_args[prop_name] = "test"
                            elif prop_type == 'number' or prop_type == 'integer':
                                test_args[prop_name] = 1
                            elif prop_type == 'boolean':
                                test_args[prop_name] = True
                            elif prop_type == 'array':
                                test_args[prop_name] = []
                            elif prop_type == 'object':
                                test_args[prop_name] = {}

                print(f"使用参数: {json.dumps(test_args, ensure_ascii=False, indent=2)}")

                try:
                    result = await client.call_tool(first_tool.name, test_args)
                    print(f"\n✅ 调用成功!")
                    print(f"结果: {json.dumps(result.content, ensure_ascii=False, indent=2)}")
                except Exception as e:
                    print(f"\n⚠️  调用失败: {str(e)}")
                    print(f"这可能是正常的，因为测试参数可能不正确")

            print("\n" + "=" * 60)
            print("✅ 测试完成!")
            print("=" * 60)

    except Exception as e:
        print(f"\n❌ 错误: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


async def test_health_endpoints():
    """测试健康检查和状态端点"""
    import httpx

    print("\n" + "=" * 60)
    print("测试 HTTP 端点")
    print("=" * 60)

    async with httpx.AsyncClient() as http_client:
        # 测试健康检查
        print("\n[Health Check] http://localhost:8000/health")
        print("-" * 60)
        try:
            response = await http_client.get("http://localhost:8000/health")
            print(f"状态码: {response.status_code}")
            print(f"响应: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
        except Exception as e:
            print(f"❌ 错误: {e}")

        # 测试状态端点
        print("\n[Status] http://localhost:8000/status")
        print("-" * 60)
        try:
            response = await http_client.get("http://localhost:8000/status")
            print(f"状态码: {response.status_code}")
            print(f"响应: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
        except Exception as e:
            print(f"❌ 错误: {e}")


async def main():
    """主函数"""
    print("\n")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 15 + "MCP Client 测试工具" + " " * 22 + "║")
    print("╚" + "═" * 58 + "╝")

    # 测试 HTTP 端点
    # await test_health_endpoints()

    # 测试 MCP 服务器
    await test_mcp_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        sys.exit(0)
