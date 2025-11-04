# AgentCore Runtime Testing Guide

This guide shows how to use the test client with AWS Bedrock AgentCore Runtime.

## Prerequisites

1. **Install boto3**
   ```bash
   pip install boto3
   ```

2. **Configure AWS Credentials**
   ```bash
   aws configure
   ```
   Or set environment variables:
   ```bash
   export AWS_ACCESS_KEY_ID=your_key_id
   export AWS_SECRET_ACCESS_KEY=your_secret_key
   export AWS_DEFAULT_REGION=us-west-2
   ```

3. **Get Your Runtime ARN**
   Your Runtime ARN is stored in `.bedrock_agentcore.yaml`:
   ```bash
   cat src/.bedrock_agentcore.yaml | grep agent_arn
   ```

   For this project, the ARN is:
   ```
   arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry-5V7YeT6HWx
   ```

## Basic Usage

### Simple Test
```bash
python test_client.py \
  --mode agentcore \
  --runtime-arn arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry-5V7YeT6HWx \
  -n 10 -c 3 \
  -p "What is machine learning?"
```

### With Environment Variable
```bash
# Set ARN once
export RUNTIME_ARN="arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry-5V7YeT6HWx"

# Use in commands
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN -n 10 -c 3
```

## Example Test Scenarios

### 1. Quick Validation Test
```bash
python test_client.py \
  --mode agentcore \
  --runtime-arn $RUNTIME_ARN \
  -n 5 -c 2 \
  -p "Hello, how are you?"
```

### 2. Stress Test
```bash
python test_client.py \
  --mode agentcore \
  --runtime-arn $RUNTIME_ARN \
  --region us-west-2 \
  -n 100 -c 20 \
  -p "Explain artificial intelligence in detail" \
  -o agentcore_stress_test.json
```

### 3. Gradual Load Test
```bash
python test_client.py \
  --mode agentcore \
  --runtime-arn $RUNTIME_ARN \
  -n 50 -c 10 \
  -d 0.2 \
  -p "What is cloud computing?" \
  -o agentcore_gradual_load.json
```

### 4. Burst Test
```bash
python test_client.py \
  --mode agentcore \
  --runtime-arn $RUNTIME_ARN \
  -n 30 -c 30 \
  -p "Explain quantum computing"
```

### 5. Extended Timeout Test
```bash
python test_client.py \
  --mode agentcore \
  --runtime-arn $RUNTIME_ARN \
  -n 10 -c 5 \
  -p "Write a comprehensive essay about climate change" \
  -t 600 \
  -o agentcore_long_running.json
```

### 6. Session Persistence Test (Fixed Session)
```bash
# Test with fixed session ID to enable memory and context persistence
python test_client.py \
  --mode agentcore \
  --runtime-arn $RUNTIME_ARN \
  --fixed-session \
  -n 10 -c 3 \
  -p "Remember this: my name is Alice. What is my name?" \
  -o agentcore_session_test.json
```

## How It Works

### AgentCore Invocation Process

1. **Session ID Management**: Two modes available

   **Default Mode** - Unique session per request:
   ```python
   session_id = str(uuid.uuid4()) + str(uuid.uuid4())[:5]  # 41 chars
   ```

   **Fixed Session Mode** - Same session for all requests (use `--fixed-session`):
   ```python
   session_id = "agentcore-load-test-session-12345"  # 37 chars
   ```

   Benefits of fixed session:
   - Enables AgentCore Memory persistence across requests
   - Maintains conversation context
   - Allows testing of memory-enabled features
   - Easier debugging in logs

2. **Payload Format**: Same as HTTP mode
   ```json
   {
     "input": {
       "prompt": "Your message here"
     }
   }
   ```

3. **Boto3 Client Call**:
   ```python
   response = client.invoke_agent_runtime(
       agentRuntimeArn=runtime_arn,
       runtimeSessionId=session_id,
       payload=payload_json,
       qualifier="DEFAULT"
   )
   ```

4. **Response Handling**: Response body is read and parsed
   ```python
   response_body = response['response'].read()
   response_data = json.loads(response_body)
   ```

## Comparing HTTP vs AgentCore

### Test Both Modes
```bash
# Set variables
export RUNTIME_ARN="arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry-5V7YeT6HWx"
export TEST_PROMPT="What is artificial intelligence?"

# Test HTTP endpoint
python test_client.py \
  --mode http \
  --url http://localhost:8080 \
  -n 100 -c 20 \
  -p "$TEST_PROMPT" \
  -o http_test.json

# Test AgentCore Runtime
python test_client.py \
  --mode agentcore \
  --runtime-arn $RUNTIME_ARN \
  -n 100 -c 20 \
  -p "$TEST_PROMPT" \
  -o agentcore_test.json

# Compare results
echo "=== HTTP Results ==="
cat http_test.json | jq '.statistics | {
  total: .total_requests,
  successful: .successful_requests,
  avg_time: .avg_response_time,
  p95: .p95_response_time,
  rps: .requests_per_second
}'

echo "\n=== AgentCore Results ==="
cat agentcore_test.json | jq '.statistics | {
  total: .total_requests,
  successful: .successful_requests,
  avg_time: .avg_response_time,
  p95: .p95_response_time,
  rps: .requests_per_second
}'
```

## Output Differences

### HTTP Mode Output
```
======================================================================
Starting Concurrent Load Test
======================================================================
Target URL: http://localhost:8080
Total Requests: 10
Concurrent Workers: 5
Prompt: What is machine learning?
======================================================================

Server Health: HEALTHY
Active Tasks: 0

✓ Request 1: 2.34s
✓ Request 2: 2.45s
...
```

### AgentCore Mode Output
```
======================================================================
Starting Concurrent Load Test
======================================================================
Target: AWS Bedrock AgentCore Runtime
Runtime ARN: arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry-5V7YeT6HWx
Region: us-west-2
Total Requests: 10
Concurrent Workers: 5
Prompt: What is machine learning?
======================================================================

✓ Request 1: 2.56s
✓ Request 2: 2.61s
...
```

## Troubleshooting

### Issue: "boto3 is required"
```
ERROR: boto3 is required for AgentCore mode. Install with: pip install boto3
```
**Solution**: Install boto3
```bash
pip install boto3
```

### Issue: "runtime_arn is required"
```
error: --runtime-arn is required when using --mode=agentcore
```
**Solution**: Provide Runtime ARN
```bash
python test_client.py --mode agentcore --runtime-arn <your-arn> -n 10 -c 5
```

### Issue: AWS Credentials Not Found
```
botocore.exceptions.NoCredentialsError: Unable to locate credentials
```
**Solution**: Configure AWS credentials
```bash
aws configure
# or
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret
```

### Issue: Access Denied
```
An error occurred (AccessDeniedException) when calling the InvokeAgentRuntime operation
```
**Solution**: Check IAM permissions. Your IAM user/role needs:
- `bedrock-agentcore:InvokeAgentRuntime` permission
- Access to the specific Runtime ARN

### Issue: Invalid ARN Format
```
Parameter validation failed: Invalid value for parameter agentRuntimeArn
```
**Solution**: Verify ARN format
```bash
# Correct format:
arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry-5V7YeT6HWx

# Get from config:
cat src/.bedrock_agentcore.yaml | grep agent_arn
```

## Performance Considerations

1. **Concurrency Limits**: AgentCore Runtime has its own scaling limits
2. **Cold Starts**: First requests may be slower due to initialization
3. **Network Latency**: AgentCore adds network overhead compared to local HTTP
4. **Session Management**:
   - Default: Each request uses unique session (isolated, no memory)
   - `--fixed-session`: All requests share same session (memory persistence enabled)

## Best Practices

1. **Start Small**: Begin with low concurrency (e.g., `-c 5`)
2. **Monitor Costs**: AgentCore invocations incur AWS charges
3. **Use Regions Wisely**: Test in the same region as your runtime
4. **Save Results**: Always use `-o` to save test results
5. **Gradual Ramp-Up**: Use `-d` flag for gradual load increase

## Quick Reference Commands

```bash
# Set environment
export RUNTIME_ARN="arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry-5V7YeT6HWx"

# Quick test (unique sessions)
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN -n 5 -c 2

# Quick test (fixed session - memory enabled)
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN --fixed-session -n 5 -c 2

# Stress test
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN -n 100 -c 20 -o stress.json

# Memory persistence test
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN --fixed-session \
  -n 10 -c 3 -p "My name is Bob. What is my name?" -o memory_test.json

# Analyze results
cat stress.json | jq '.statistics'

# Compare with HTTP
python test_client.py --mode http -n 100 -c 20 -o http.json
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN -n 100 -c 20 -o agentcore.json
diff <(cat http.json | jq '.statistics') <(cat agentcore.json | jq '.statistics')
```
