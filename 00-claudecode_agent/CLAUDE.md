# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Claude Code AgentCore Runtime example that demonstrates how to run Claude Code as an AWS Bedrock AgentCore runtime agent. The agent specializes in building and deploying Flask web applications to AWS Elastic Beanstalk.

## Architecture

### Core Components

**Entry Points:**
- `claude_code_agent.py` - Simple streaming implementation with basic MCP server integration
- `claude_code_agent_2.py` - Advanced implementation with session management, request handling, and DynamoDB integration (used in production)

**Key Modules:**
- `data_types.py` - Pydantic models for request/response handling (OperationsRequest, ChatCompletionRequest, etc.)
- `utils.py` - Utilities for DynamoDB operations, user session management, MCP server configuration management
- `mcp/eb_server.py` - MCP server providing Elastic Beanstalk deployment tools

### Agent Flow

1. **Initialization**: Agent loads MCP server configurations (Elastic Beanstalk + optional Context7)
2. **Request Handling**: Processes three request types via `OperationsRequest`:
   - `chatcompletion` - Main conversation flow with streaming responses
   - `stopstream` - Cancels running agent tasks
   - `removehistory` - Clears conversation history and disconnects client
3. **Claude SDK Integration**: Uses `claude-agent-sdk` to manage Claude Code CLI interactions
4. **Streaming**: Converts Claude Code messages to SSE-compatible format via `pull_queue_stream()`

### MCP Server Configuration

The agent dynamically configures MCP servers:
- **Elastic Beanstalk MCP**: Provides `deploy_on_eb_from_path()` tool for deploying Flask apps
- **Context7 MCP** (optional): HTTP-based MCP for library documentation access
- User-specific MCP servers loaded from DynamoDB or local JSON config

## Environment Setup

### Required Environment Variables (.env)

```bash
# Bedrock API Authentication
AWS_BEARER_TOKEN_BEDROCK=<bedrock-api-key>

# Claude Code Configuration
CLAUDE_CODE_USE_BEDROCK=1
CLAUDE_CODE_MAX_OUTPUT_TOKENS=16000
MAX_THINKING_TOKENS=1024

# Optional: Context7 for documentation access
CONTEXT7_API_KEY=<context7-api-key>

# AWS Configuration
AWS_REGION=us-west-2  # default if not set
```

### AWS Resources Setup

Run the setup script to create necessary AWS resources:

```bash
chmod +x pre_setup.sh
./pre_setup.sh
```

This creates:
1. IAM execution role for AgentCore: `agentcore-claude_code_agent-role`
2. ECR repository: `bedrock_agentcore-claude_code_agent`
3. Elastic Beanstalk service role: `aws-elasticbeanstalk-service-role`
4. Elastic Beanstalk EC2 role: `aws-elasticbeanstalk-ec2-role`

## Development Commands

### Package Management

```bash
# Install dependencies
uv sync

# Run with uv
uv run python claude_code_agent_2.py
```

### AgentCore Configuration & Deployment

```bash
# Configure AgentCore (after running pre_setup.sh)
# Replace <YOUR_IAM_ROLE_ARN> with the ARN from pre_setup.sh output
uv run agentcore config --entrypoint claude_code_agent.py -er <YOUR_IAM_ROLE_ARN>

# Build and launch to AgentCore
uv run agentcore launch
```

### Testing the Agent

```bash
# Invoke the agent with a sample prompt
uv run agentcore invoke '{"model":"us.anthropic.claude-3-7-sonnet-20250219-v1:0", "prompt": "create a interactive learning website to introduce Transformer in AI, targeting middle school students"}'
```

## Key Implementation Details

### Working Directory Constraints

The agent is **restricted to `/app/workspace/`** for all operations. All project files, Flask applications, and deployments must be created within this directory structure.

### Port Configuration for Flask Apps

When building Flask applications for Elastic Beanstalk deployment:
- Applications **must** run on **port 8000** (Elastic Beanstalk's default nginx upstream port)
- Use environment variable pattern: `os.environ.get('PORT', 8000)`
- Bind to `0.0.0.0` for proper container networking

### System Prompt Philosophy

The default system prompt (DEFAULT_SYSTEM in both agent files) configures Claude as an "expert web application developer specializing in AWS Elastic Beanstalk deployments" with emphasis on:
- Using Context7 MCP for dependency validation
- Following Elastic Beanstalk best practices
- Building Flask-based web servers

### Session Management (claude_code_agent_2.py)

- Uses global `claude_client` singleton with cleanup monitoring
- `cleanup_monitor()` background task handles proper client disconnection
- Session locking via `session_lock` (threading.RLock) prevents race conditions
- DynamoDB or local JSON for persistent user configurations

### Message Streaming Format

The agent converts Claude SDK messages to SSE (Server-Sent Events) format:
- `message_start` - Begin assistant response
- `block_delta` - Content chunks (text, reasoning_content, toolinput_content)
- `block_stop` - End content block
- `message_stop` - End turn (with stopReason: tool_use/end_turn)
- `result_pairs` - Tool execution results with toolUseId mapping

### Allowed/Disallowed Tools

**claude_code_agent.py** (simple version):
- Allowed: MCP tools + Read, Write, TodoWrite, Task, LS, Bash, Edit, Grep, Glob
- Disallowed: Bash(rm*), KillBash

**claude_code_agent_2.py** (production version):
- Allowed: TodoWrite, Task, WebFetch, WebSearch + user-configured MCP tools
- Disallowed: Bash, KillBash, Read, Write, LS, Glob, Grep, NotebookEditCell, Edit, MultiEdit

## Docker Deployment

The Dockerfile uses:
- Base: `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` (ARM64)
- Installs Node.js 22.x for Claude Code CLI
- Exposes port 8080
- Entry point: `claude_code_agent_2.py`

## Elastic Beanstalk Deployment Tool

The MCP server (`mcp/eb_server.py`) provides `deploy_on_eb_from_path(proj_dir)` which:
1. Adds `.ebextensions/python.config` with WSGIPath configuration
2. Zips the project directory
3. Uploads to S3 bucket (auto-created with naming: `eb-deploy-{region}-{account_id}`)
4. Creates EB application version
5. Deploys to new EB environment with auto-selected Python solution stack
6. Waits for deployment completion (timeout: 600s)
7. Returns public URL of deployed application

## Dependencies

Key packages (from pyproject.toml):
- `bedrock-agentcore>=0.1.2` - AgentCore runtime framework
- `bedrock-agentcore-starter-toolkit>=0.1.6` - AgentCore utilities
- `claude-agent-sdk>=0.1.1` - Claude Code SDK for Python
- `mcp>=1.13.0` - Model Context Protocol implementation
- `boto3>=1.40.13` - AWS SDK
- `python-dotenv>=1.0.1` - Environment variable management
