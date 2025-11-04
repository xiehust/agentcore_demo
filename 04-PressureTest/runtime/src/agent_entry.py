from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any
from datetime import datetime, timezone
from strands import Agent
from concurrent.futures import ThreadPoolExecutor
import asyncio
from functools import partial
import logging
from contextlib import asynccontextmanager
from strands.models import BedrockModel
from botocore.config import Config
import psutil
import os

MODEL_ID = "qwen.qwen3-coder-480b-a35b-v1:0"
agent_model = None
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Thread pool for concurrent agent processing
# Max workers can be configured based on your requirements
MAX_WORKERS = 100
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="agent-worker")

# Track active tasks for ping status
active_tasks = {}
active_tasks_lock = asyncio.Lock()
last_status_update_time = datetime.now(timezone.utc)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Lifespan context manager for startup and shutdown events.
    Manages thread pool lifecycle.
    """
    # Startup
    logger.info(f"Starting Strands Agent Server with {MAX_WORKERS} worker threads")
    yield
    # Shutdown
    logger.info("Shutting down thread pool...")
    executor.shutdown(wait=True)
    logger.info("Thread pool shutdown complete")

app = FastAPI(
    title="Strands Agent Server",
    version="1.0.0",
    lifespan=lifespan
)

class InvocationRequest(BaseModel):
    input: Dict[str, Any]

class InvocationResponse(BaseModel):
    output: Dict[str, Any]

async def add_active_task(request_id: str):
    """Add task to active tasks tracking"""
    global last_status_update_time
    async with active_tasks_lock:
        active_tasks[request_id] = datetime.now(timezone.utc)
        last_status_update_time = datetime.now(timezone.utc)
        logger.debug(f"Active tasks count: {len(active_tasks)}")

async def remove_active_task(request_id: str):
    """Remove task from active tasks tracking"""
    global last_status_update_time
    async with active_tasks_lock:
        if request_id in active_tasks:
            del active_tasks[request_id]
            last_status_update_time = datetime.now(timezone.utc)
            logger.debug(f"Active tasks count: {len(active_tasks)}")

def process_agent_request(user_message: str, request_id: str) -> Dict[str, Any]:
    """
    Process agent request in a separate thread.
    Each invocation creates a new Agent instance to ensure isolation.

    Args:
        user_message: User's prompt message
        request_id: Unique request identifier for logging

    Returns:
        Response dictionary with agent result
    """
    try:
        logger.info(f"Request {request_id}: Starting agent processing")

        # Create a new Agent instance for this request to ensure thread safety
        agent = Agent(model=agent_model)

        # Process the message
        result = agent(user_message)

        logger.info(f"Request {request_id}: Agent processing completed")

        return {
            "message": result.message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "metrics":{"accumulated_metrics": result.metrics.accumulated_metrics, "accumulated_usage":result.metrics.accumulated_usage},
            "status": "success"
        }

    except Exception as e:
        logger.error(f"Request {request_id}: Agent processing failed - {str(e)}")
        raise

async def get_stats_data() -> Dict[str, Any]:
    """Get thread pool, task statistics, CPU and memory usage"""
    async with active_tasks_lock:
        active_count = len(active_tasks)

    # Get current process
    process = psutil.Process(os.getpid())

    # Get CPU usage (percent over short interval)
    cpu_percent = process.cpu_percent(interval=0.1)

    # Get memory info
    memory_info = process.memory_info()
    memory_mb = memory_info.rss / (1024 * 1024)  # Convert bytes to MB
    memory_percent = process.memory_percent()

    # Get system-wide stats
    system_cpu_percent = psutil.cpu_percent(interval=0.1)
    system_memory = psutil.virtual_memory()

    return {
        "max_workers": MAX_WORKERS,
        "active_threads": executor._threads.__len__() if executor._threads else 0,
        "active_tasks": active_count,
        "status": "operational",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "process": {
            "cpu_percent": round(cpu_percent, 2),
            "memory_mb": round(memory_mb, 2),
            "memory_percent": round(memory_percent, 2),
            "pid": os.getpid()
        },
        "system": {
            "cpu_percent": round(system_cpu_percent, 2),
            "memory_total_mb": round(system_memory.total / (1024 * 1024), 2),
            "memory_used_mb": round(system_memory.used / (1024 * 1024), 2),
            "memory_available_mb": round(system_memory.available / (1024 * 1024), 2),
            "memory_percent": round(system_memory.percent, 2)
        }
    }

@app.post("/invocations", response_model=InvocationResponse)
async def invoke_agent(request: InvocationRequest):
    """
    Handle agent invocation requests with concurrent processing.
    Each request is processed in a separate thread from the thread pool.

    Special requests:
    - If input contains 'get_stats': true, returns statistics instead of processing agent
    """
    
    global agent_model
    try:
        # Check if this is a stats request
        if request.input.get("get_stats") is True:
            logger.info("Stats request received via invocations endpoint")
            stats_data = await get_stats_data()
            return InvocationResponse(output={
                "type": "stats",
                "data": stats_data
            })

        user_message = request.input.get("prompt", "")
        if not user_message:
            raise HTTPException(
                status_code=400,
                detail="No prompt found in input. Please provide a 'prompt' key in the input."
            )

        if not agent_model:
            MAX_TOKENS = 4096
            TEMPERATURE = 0.0
            agent_model = BedrockModel(
                            model_id=request.input.get("model_id", MODEL_ID) ,
                            max_tokens=request.input.get("max_tokens",MAX_TOKENS),
                            temperature=request.input.get("temperature",TEMPERATURE),
                            boto_client_config=Config(
                                        read_timeout=1800,
                                        connect_timeout=30,
                                        retries=dict(max_attempts=3, mode="adaptive"),
                                        ),
                        )
        # Generate unique request ID for tracking
        request_id = f"{datetime.now(timezone.utc).timestamp()}"

        logger.info(f"Request {request_id}: Received invocation request")

        # Add task to active tasks tracking
        await add_active_task(request_id)

        try:
            # Submit task to thread pool and await result asynchronously
            # This ensures the endpoint doesn't block while waiting for agent processing
            loop = asyncio.get_event_loop()
            process_func = partial(process_agent_request, user_message, request_id)

            # Run the blocking agent processing in thread pool
            response = await loop.run_in_executor(executor, process_func)

            logger.info(f"Request {request_id}: Response sent to client")

            return InvocationResponse(output=response)
        finally:
            # Remove task from active tasks tracking
            await remove_active_task(request_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Invocation failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Agent processing failed: {str(e)}")

@app.get("/ping")
async def ping():
    """
    Health check endpoint with busy status detection.
    Returns HEALTHY when no tasks are running, HEALTHY_BUSY when tasks are active.
    """
    async with active_tasks_lock:
        active_count = len(active_tasks)
        status = "HEALTHY_BUSY" if active_count > 0 else "HEALTHY"

        return {
            "status": status,
            "timeOfLastUpdate": last_status_update_time.isoformat(),
            "activeTasks": active_count
        }

@app.get("/stats")
async def get_stats():
    """Get thread pool and task statistics (HTTP GET endpoint)"""
    return await get_stats_data()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)