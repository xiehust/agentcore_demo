# AgentCore Demo

A demonstration repository for showcasing agent capabilities and features.

## Overview

This repository contains examples and demonstrations of agent-based systems.

## Getting Started

Clone the repository and explore the examples to learn more about agent capabilities. See [CONTRIBUTING.md](CONTRIBUTING.md) for how to build and run a demo.

## Features

- Agent demonstrations
- Core functionality examples
- Integration patterns

## Demos

### 1. ClaudeCode Agent

The [ClaudeCode Agent](claudecode_agent/README.md) demonstrates how Claude can run in AgentCore runtime for software development tasks, helping with code generation, bug fixing, and technical guidance.

### 19. AgentCore Gateway Token Exchange

The [AgentCore Gateway Token Exchange demo](19-gateway-token-exchange/README.md) demonstrates RFC 8693 on-behalf-of token exchange with a real AgentCore Gateway, OAuth2 Credential Provider, KMS-signed JWTs, and a protected MCP server.

### 20. AgentCore Identity Consent Portal

The [Consent Portal introduction](20-consent-portal/README.md) explains the AWS-managed consent portal for Gateway 3LO flows and compares it with the previous OAuth2 Credential Provider approach that required a self-hosted callback endpoint and `CompleteResourceTokenAuth` session binding.

## FAQ

**What is this repository for?**
It collects hands-on demos and experiments for Amazon Bedrock AgentCore, such as Runtime, Gateway and Identity.

**How are the demos organized?**
Each numbered directory is a self-contained demo. Start with the `README.md` inside that directory for its prerequisites, setup and results.

**What do I need to run a demo?**
An AWS account with access to Bedrock AgentCore and configured AWS credentials. Individual demos may need extra tools or settings, which their READMEs list.

**Will running a demo cost money?**
Most demos create real AWS resources. Follow the demo's cleanup steps when you finish to avoid ongoing charges.

**Under what license is this code shared?**
The [MIT License](LICENSE).
