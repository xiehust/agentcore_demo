import asyncio
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Initialize app with debug mode for task management
app = BedrockAgentCoreApp()

task_running = False

@app.async_task
async def background_work():
    global task_running
    task_running = True
    await asyncio.sleep(3600*8)  # Status becomes "HealthyBusy"
    print("🚀 Background work is done!")
    task_running = False
    return "work has been done"

@app.entrypoint
async def handler(payload,context):
    prompt = payload.get("prompt", "start")
    ping = payload.get("action")
    if prompt == "start":
        if not task_running:
            asyncio.create_task(background_work())
            return {"status": "started"}
        else:
            return {"status": "already running"}
    elif ping == 'ping':
        return {"status": "still running"} if task_running else {"status": "idle"}

if __name__ == "__main__":
    print("🚀 Simple Async Strands Example")
    app.run()