# Contributing

Thanks for your interest in improving AgentCore Demo. Each numbered directory
(`00-claudecode_agent`, `01-async-agent`, ...) is a self-contained demo with its
own dependencies and README.

## How to build

Prerequisites:

- Python 3.10+ (check the demo's `pyproject.toml`; many need 3.11 or 3.13)
- [uv](https://docs.astral.sh/uv/)
- AWS credentials with Amazon Bedrock AgentCore access
- Docker with `linux/arm64` support, only for demos that ship a `Dockerfile`

Build and run a demo:

```bash
cd <demo-directory>

# Install dependencies
uv sync                              # demos with pyproject.toml
uv venv && uv pip install -r requirements.txt   # demos with only requirements.txt

# Run locally
uv run python <entry-point>.py

# Deploy to AgentCore Runtime (demos that support it)
uv run agentcore configure
uv run agentcore deploy   # older demos use `agentcore launch`
```

Some demos need extra setup, such as copying an `env.sample` file to `.env`.
Follow that demo's README for its exact commands.

## Submitting changes

- Keep changes scoped to one demo where possible.
- Never commit credentials, `.env` files or `.bedrock_agentcore.yaml`.
- Run the demo's tests or linters if it has them, for example `uv run pytest`.
- Open a pull request describing what changed and how you verified it.
