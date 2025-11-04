#!/usr/bin/env python3
"""
Concurrent test client for AgentCore Runtime
Performs load testing with configurable concurrency and request parameters
Supports both HTTP and AWS Bedrock AgentCore Runtime invocation
"""

import asyncio
import aiohttp
import time
import argparse
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
import statistics
import uuid
import boto3
from botocore.config import Config


try:
    import boto3
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False


@dataclass
class RequestMetrics:
    """Metrics from agent response"""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class RequestResult:
    """Single request result"""
    request_id: int
    success: bool
    duration: float  # seconds
    status_code: int
    error_message: str = ""
    response_data: Dict[str, Any] | None = None
    metrics: RequestMetrics | None = None


@dataclass
class TestStatistics:
    """Test run statistics"""
    total_requests: int
    successful_requests: int
    failed_requests: int
    total_duration: float
    avg_response_time: float
    min_response_time: float
    max_response_time: float
    median_response_time: float
    p95_response_time: float
    p99_response_time: float
    requests_per_second: float
    concurrent_workers: int
    # Metrics statistics
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    avg_latency_ms: float = 0
    avg_input_tokens: float = 0
    avg_output_tokens: float = 0


class ConcurrentTestClient:
    """Concurrent test client for load testing"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: int = 300,
        runtime_arn: Optional[str] = None,
        region: Optional[str] = None,
        use_agentcore: bool = False,
        fixed_session: bool = False,
        bearer_token: Optional[str] = None
    ):
        self.base_url = base_url.rstrip('/') if base_url else None
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.results: List[RequestResult] = []
        self.use_agentcore = use_agentcore
        self.runtime_arn = runtime_arn
        self.region = region or 'us-west-2'
        self.fixed_session = fixed_session
        self.fixed_session_id = "agentcore-load-test-session-12345"  # 37 chars, meets 33+ requirement
        self.bearer_token = bearer_token

        # Initialize boto3 client for AgentCore if needed
        if self.use_agentcore:
            if not BOTO3_AVAILABLE:
                raise ImportError("boto3 is required for AgentCore invocation. Install with: pip install boto3")
            if not self.runtime_arn:
                raise ValueError("runtime_arn is required when use_agentcore=True")
            self.agentcore_client = boto3.client('bedrock-agentcore', 
                                                 region_name=self.region,
                                                 config = Config(
                                                     read_timeout=300,
                                                     max_pool_connections=100))
            print(f"Using AWS Bedrock AgentCore Runtime: {self.runtime_arn}")
            if self.fixed_session:
                print(f"Using fixed session ID: {self.fixed_session_id}")
        else:
            if not self.base_url:
                raise ValueError("base_url is required when use_agentcore=False")
            print(f"Using HTTP endpoint: {self.base_url}")
            if self.fixed_session:
                print(f"Using fixed session ID: {self.fixed_session_id}")

    def stop_session(self):
        if self.fixed_session:
            boto3_client = boto3.client("bedrock-agentcore")
            
            try:
                boto3_client.stop_runtime_session(runtimeSessionId=self.fixed_session_id,
                                                   agentRuntimeArn=self.runtime_arn)
                print(f"Stopped session: {self.fixed_session_id}")
            except Exception as e:
                print(f"Failed to stop session: {e}")
                
    def invoke_agentcore_sync(
        self,
        request_id: int,
        prompt: str,
        get_stats: bool = False
    ) -> RequestResult:
        """
        Invoke AgentCore Runtime synchronously (runs in thread pool)

        Args:
            request_id: Unique request identifier
            prompt: User prompt to send
            get_stats: If True, request stats instead of agent processing

        Returns:
            RequestResult object
        """
        if get_stats:
            payload_dict = {"input": {"get_stats": True}}
        else:
            payload_dict = {"input": {"prompt": prompt}}

        payload = json.dumps(payload_dict)

        # Generate or use fixed session ID (must be 33+ chars)
        if self.fixed_session:
            # Use fixed session ID for session persistence and memory tracking
            session_id = self.fixed_session_id
        else:
            # Generate unique session ID for each request
            session_id = str(uuid.uuid4()) + str(uuid.uuid4())[:5]

        start_time = time.time()

        try:
            response = self.agentcore_client.invoke_agent_runtime(
                agentRuntimeArn=self.runtime_arn,
                runtimeSessionId=session_id,
                payload=payload,
                qualifier="DEFAULT"
            )

            duration = time.time() - start_time

            # Read response body
            response_body = response['response'].read()
            response_data = json.loads(response_body)

            # Extract metrics from response
            metrics = None
            if response_data and 'output' in response_data:
                output = response_data['output']
                if 'metrics' in output:
                    metrics_data = output['metrics']
                    accumulated_metrics = metrics_data.get('accumulated_metrics', {})
                    accumulated_usage = metrics_data.get('accumulated_usage', {})

                    metrics = RequestMetrics(
                        latency_ms=accumulated_metrics.get('latencyMs', 0),
                        input_tokens=accumulated_usage.get('inputTokens', 0),
                        output_tokens=accumulated_usage.get('outputTokens', 0),
                        total_tokens=accumulated_usage.get('totalTokens', 0)
                    )

            result = RequestResult(
                request_id=request_id,
                success=True,
                duration=duration,
                status_code=200,
                response_data=response_data,
                metrics=metrics
            )

            if metrics:
                print(f"✓ Request {request_id}: {duration:.2f}s (tokens: {metrics.input_tokens}/{metrics.output_tokens}, latency: {metrics.latency_ms}ms)")
            else:
                print(f"✓ Request {request_id}: {duration:.2f}s")
            return result

        except Exception as e:
            duration = time.time() - start_time
            print(f"✗ Request {request_id}: {str(e)}")
            return RequestResult(
                request_id=request_id,
                success=False,
                duration=duration,
                status_code=0,
                error_message=str(e)
            )

    async def send_request(
        self,
        session: aiohttp.ClientSession,
        request_id: int,
        prompt: str,
        get_stats: bool = False
    ) -> RequestResult:
        """
        Send a single request to the invocations endpoint (HTTP)

        Args:
            session: aiohttp session
            request_id: Unique request identifier
            prompt: User prompt to send
            get_stats: If True, request stats instead of agent processing

        Returns:
            RequestResult object
        """
        url = f"{self.base_url}/invocations"

        if get_stats:
            payload = {"input": {"get_stats": True}}
        else:
            payload = {"input": {"prompt": prompt}}

        # Prepare headers with optional bearer token
        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
            headers["Content-Type"] = "application/json"
        
        if self.fixed_session:
            headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] = self.fixed_session_id

        start_time = time.time()

        try:
            async with session.post(url, json=payload, headers=headers) as response:
                duration = time.time() - start_time
                response_data = await response.json()

                # Extract metrics from response
                metrics = None
                if response.status == 200 and response_data and 'output' in response_data:
                    output = response_data['output']
                    if 'metrics' in output:
                        metrics_data = output['metrics']
                        accumulated_metrics = metrics_data.get('accumulated_metrics', {})
                        accumulated_usage = metrics_data.get('accumulated_usage', {})

                        metrics = RequestMetrics(
                            latency_ms=accumulated_metrics.get('latencyMs', 0),
                            input_tokens=accumulated_usage.get('inputTokens', 0),
                            output_tokens=accumulated_usage.get('outputTokens', 0),
                            total_tokens=accumulated_usage.get('totalTokens', 0)
                        )

                result = RequestResult(
                    request_id=request_id,
                    success=response.status == 200,
                    duration=duration,
                    status_code=response.status,
                    response_data=response_data,
                    metrics=metrics
                )

                if result.success:
                    if metrics:
                        print(f"✓ Request {request_id}: {duration:.2f}s (tokens: {metrics.input_tokens}/{metrics.output_tokens}, latency: {metrics.latency_ms}ms)")
                    else:
                        print(f"✓ Request {request_id}: {duration:.2f}s")
                else:
                    print(f"✗ Request {request_id}: HTTP {response.status}")

                return result

        except asyncio.TimeoutError:
            duration = time.time() - start_time
            print(f"✗ Request {request_id}: Timeout after {duration:.2f}s")
            return RequestResult(
                request_id=request_id,
                success=False,
                duration=duration,
                status_code=0,
                error_message="Timeout"
            )
        except Exception as e:
            duration = time.time() - start_time
            print(f"✗ Request {request_id}: {str(e)}")
            return RequestResult(
                request_id=request_id,
                success=False,
                duration=duration,
                status_code=0,
                error_message=str(e)
            )

    async def check_health(self) -> Dict[str, Any]:
        """Check server health via /ping endpoint (HTTP only)"""
        if self.use_agentcore:
            return {"status": "AgentCore", "message": "Health check not applicable for AgentCore Runtime"}

        url = f"{self.base_url}/ping"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data
                    else:
                        return {"status": "ERROR", "http_code": response.status}
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    async def get_stats(self) -> Dict[str, Any]:
        """Get server statistics via /stats endpoint (HTTP only)"""
        if self.use_agentcore:
            return {"message": "Stats endpoint not applicable for AgentCore Runtime"}

        url = f"{self.base_url}/stats"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        return {"error": f"HTTP {response.status}"}
        except Exception as e:
            return {"error": str(e)}

    async def run_concurrent_test(
        self,
        num_requests: int,
        concurrent_workers: int,
        prompt: str,
        delay_between_requests: float = 0
    ) -> TestStatistics:
        """
        Run concurrent load test

        Args:
            num_requests: Total number of requests to send
            concurrent_workers: Number of concurrent workers
            prompt: Prompt to send in each request
            delay_between_requests: Delay in seconds between launching requests

        Returns:
            TestStatistics object
        """
        print(f"\n{'='*70}")
        print(f"Starting Concurrent Load Test")
        print(f"{'='*70}")
        if self.use_agentcore:
            print(f"Target: AWS Bedrock AgentCore Runtime")
            print(f"Runtime ARN: {self.runtime_arn}")
            print(f"Region: {self.region}")
        else:
            print(f"Target URL: {self.base_url}")
        print(f"Total Requests: {num_requests}")
        print(f"Concurrent Workers: {concurrent_workers}")
        print(f"Prompt: {prompt[:50]}..." if len(prompt) > 50 else f"Prompt: {prompt}")
        print(f"{'='*70}\n")

        # Check server health before starting (HTTP only)
        if not self.use_agentcore:
            health = await self.check_health()
            print(f"Server Health: {health.get('status', 'UNKNOWN')}")
            if health.get('activeTasks'):
                print(f"Active Tasks: {health.get('activeTasks')}")
            print()

        self.results = []
        test_start_time = time.time()

        if self.use_agentcore:
            # Use AgentCore invocation with thread pool
            loop = asyncio.get_event_loop()
            semaphore = asyncio.Semaphore(concurrent_workers)

            async def limited_agentcore_request(req_id: int):
                async with semaphore:
                    print(f"start request:[{req_id}]")
                    return await loop.run_in_executor(
                        None,
                        self.invoke_agentcore_sync,
                        req_id,
                        prompt,
                        False
                    )

            # Create tasks with optional delay
            tasks = []
            for i in range(num_requests):
                if delay_between_requests > 0 and i > 0:
                    await asyncio.sleep(delay_between_requests)
                task = asyncio.create_task(limited_agentcore_request(i + 1))
                tasks.append(task)

            # Wait for all requests to complete
            self.results = await asyncio.gather(*tasks)

        else:
            # Use HTTP invocation
            semaphore = asyncio.Semaphore(concurrent_workers)

            async def limited_request(req_id: int):
                async with semaphore:
                    print(f"start request:[{req_id}]")
                    return await self.send_request(session, req_id, prompt)

            # Create session and run all requests
            connector = aiohttp.TCPConnector(limit=concurrent_workers * 2)
            async with aiohttp.ClientSession(
                timeout=self.timeout,
                connector=connector
            ) as session:
                # Create tasks with optional delay
                tasks = []
                for i in range(num_requests):
                    if delay_between_requests > 0 and i > 0:
                        await asyncio.sleep(delay_between_requests)
                    task = asyncio.create_task(limited_request(i + 1))
                    tasks.append(task)

                # Wait for all requests to complete
                self.results = await asyncio.gather(*tasks)

        test_duration = time.time() - test_start_time

        # Calculate statistics
        stats = self._calculate_statistics(
            self.results,
            test_duration,
            concurrent_workers
        )

        # Get final server stats (HTTP only)
        if not self.use_agentcore:
            print(f"\n{'='*70}")
            final_stats = await self.get_stats()
            print(f"Final Server Stats: {json.dumps(final_stats, indent=2)}")

        return stats

    def _calculate_statistics(
        self,
        results: List[RequestResult],
        total_duration: float,
        concurrent_workers: int
    ) -> TestStatistics:
        """Calculate test statistics from results"""
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        success_durations = [r.duration for r in successful]

        if not success_durations:
            success_durations = [0]

        sorted_durations = sorted(success_durations)
        n = len(sorted_durations)

        # Calculate metrics statistics
        results_with_metrics = [r for r in successful if r.metrics is not None]

        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        total_latency_ms = 0

        if results_with_metrics:
            for r in results_with_metrics:
                if r.metrics:  # Type guard
                    total_input_tokens += r.metrics.input_tokens
                    total_output_tokens += r.metrics.output_tokens
                    total_tokens += r.metrics.total_tokens
                    total_latency_ms += r.metrics.latency_ms

            avg_latency_ms = total_latency_ms / len(results_with_metrics)
            avg_input_tokens = total_input_tokens / len(results_with_metrics)
            avg_output_tokens = total_output_tokens / len(results_with_metrics)
        else:
            avg_latency_ms = 0
            avg_input_tokens = 0
            avg_output_tokens = 0

        stats = TestStatistics(
            total_requests=len(results),
            successful_requests=len(successful),
            failed_requests=len(failed),
            total_duration=total_duration,
            avg_response_time=statistics.mean(success_durations),
            min_response_time=min(success_durations),
            max_response_time=max(success_durations),
            median_response_time=statistics.median(success_durations),
            p95_response_time=sorted_durations[int(n * 0.95)] if n > 0 else 0,
            p99_response_time=sorted_durations[int(n * 0.99)] if n > 0 else 0,
            requests_per_second=len(results) / total_duration if total_duration > 0 else 0,
            concurrent_workers=concurrent_workers,
            # Metrics
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_tokens=total_tokens,
            avg_latency_ms=avg_latency_ms,
            avg_input_tokens=avg_input_tokens,
            avg_output_tokens=avg_output_tokens
        )

        return stats

    def print_statistics(self, stats: TestStatistics):
        """Print formatted test statistics"""
        print(f"\n{'='*70}")
        print(f"Test Results")
        print(f"{'='*70}")
        print(f"Total Requests:      {stats.total_requests}")
        print(f"Successful:          {stats.successful_requests} ({stats.successful_requests/stats.total_requests*100:.1f}%)")
        print(f"Failed:              {stats.failed_requests} ({stats.failed_requests/stats.total_requests*100:.1f}%)")
        print(f"Total Duration:      {stats.total_duration:.2f}s")
        print(f"Requests/Second:     {stats.requests_per_second:.2f}")
        print(f"\nResponse Times (seconds):")
        print(f"  Average:           {stats.avg_response_time:.3f}s")
        print(f"  Median:            {stats.median_response_time:.3f}s")
        print(f"  Min:               {stats.min_response_time:.3f}s")
        print(f"  Max:               {stats.max_response_time:.3f}s")
        print(f"  P95:               {stats.p95_response_time:.3f}s")
        print(f"  P99:               {stats.p99_response_time:.3f}s")

        # Print metrics if available
        if stats.total_tokens > 0:
            print(f"\nToken Usage:")
            print(f"  Total Input:       {stats.total_input_tokens:,} tokens")
            print(f"  Total Output:      {stats.total_output_tokens:,} tokens")
            print(f"  Total:             {stats.total_tokens:,} tokens")
            print(f"  Avg Input/Req:     {stats.avg_input_tokens:.1f} tokens")
            print(f"  Avg Output/Req:    {stats.avg_output_tokens:.1f} tokens")
            print(f"\nAgent Latency:")
            print(f"  Average:           {stats.avg_latency_ms:.1f}ms")

        print(f"{'='*70}\n")

    def save_results(self, stats: TestStatistics, output_file: str):
        """Save test results to JSON file"""
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "statistics": asdict(stats),
            "results": [asdict(r) for r in self.results]
        }

        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2, default=str)

        print(f"Results saved to: {output_file}")


async def main():
    parser = argparse.ArgumentParser(
        description="Concurrent test client for AgentCore Runtime - Supports HTTP and AWS Bedrock AgentCore"
    )

    # Runtime selection
    parser.add_argument(
        "--mode",
        choices=["http", "agentcore"],
        default="http",
        help="Invocation mode: 'http' for local HTTP endpoint, 'agentcore' for AWS Bedrock AgentCore Runtime (default: http)"
    )

    # HTTP mode arguments
    parser.add_argument(
        "--url",
        default="http://localhost:8080",
        help="Base URL of the server for HTTP mode (default: http://localhost:8080)"
    )
    parser.add_argument(
        "--bearer-token",
        help="Bearer token for HTTP authentication (optional, only for HTTP mode)"
    )

    # AgentCore mode arguments
    parser.add_argument(
        "--runtime-arn",
        help="AgentCore Runtime ARN (required for agentcore mode). Example: arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/agent_entry-xyz"
    )
    parser.add_argument(
        "--region",
        default="us-west-2",
        help="AWS region for AgentCore (default: us-west-2)"
    )
    parser.add_argument(
        "--fixed-session",
        action="store_true",
        help="Use fixed session ID for all requests (enables session persistence and memory tracking in AgentCore)"
    )

    # Common arguments
    parser.add_argument(
        "-n", "--num-requests",
        type=int,
        default=10,
        help="Total number of requests to send (default: 10)"
    )
    parser.add_argument(
        "-c", "--concurrency",
        type=int,
        default=5,
        help="Number of concurrent workers (default: 5)"
    )
    parser.add_argument(
        "-p", "--prompt",
        default="Hello, how are you?",
        help="Prompt to send in each request"
    )
    parser.add_argument(
        "-d", "--delay",
        type=float,
        default=0,
        help="Delay between launching requests in seconds (default: 0)"
    )
    parser.add_argument(
        "-t", "--timeout",
        type=int,
        default=300,
        help="Request timeout in seconds (default: 300)"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output file for results (JSON format)"
    )

    args = parser.parse_args()

    # Validate arguments based on mode
    if args.mode == "agentcore":
        if not args.runtime_arn:
            parser.error("--runtime-arn is required when using --mode=agentcore")
        if not BOTO3_AVAILABLE:
            print("ERROR: boto3 is required for AgentCore mode. Install with: pip install boto3")
            return

    # Create test client
    if args.mode == "agentcore":
        client = ConcurrentTestClient(
            runtime_arn=args.runtime_arn,
            region=args.region,
            timeout=args.timeout,
            use_agentcore=True,
            fixed_session=args.fixed_session
        )
    else:
        client = ConcurrentTestClient(
            base_url=args.url,
            timeout=args.timeout,
            use_agentcore=False,
            fixed_session=args.fixed_session,  
            bearer_token=args.bearer_token
        )

    # Run test
    stats = await client.run_concurrent_test(
        num_requests=args.num_requests,
        concurrent_workers=args.concurrency,
        prompt=args.prompt,
        delay_between_requests=args.delay
    )
    client.stop_session()
    # Print results
    client.print_statistics(stats)

    # Save results if output file specified
    if args.output:
        client.save_results(stats, args.output)


if __name__ == "__main__":
    asyncio.run(main())
