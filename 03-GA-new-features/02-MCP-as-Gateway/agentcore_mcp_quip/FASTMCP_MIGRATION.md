# FastMCP Migration Guide

## Overview
This document describes the migration from manual Starlette/MCP implementation to the FastMCP framework for the Quip MCP server.

## Files
- **Original**: `start_remote_mcp_simple.py` (287 lines)
- **New**: `start_remote_mcp_fastmcp.py` (253 lines)
- **Reduction**: ~12% code reduction with enhanced functionality

## Key Improvements

### 1. Framework Benefits
Using FastMCP provides:
- ✅ Automatic JSON-RPC protocol handling
- ✅ Built-in parameter validation
- ✅ Context injection for logging and resource access
- ✅ Middleware support for request/response processing
- ✅ Automatic tool and resource registration
- ✅ Server-Sent Events (SSE) support out of the box

### 2. Code Simplification

#### Before (Manual Implementation)
```python
# Manual JSON-RPC parsing and routing
if method == 'initialize':
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": request_data.get('id'),
        "result": {
            "protocolVersion": "2024-11-05",
            # ... more boilerplate
        }
    })
elif method == 'tools/list':
    # Manual tool listing
    return JSONResponse({...})
elif method == 'tools/call':
    # Manual tool dispatch
    # ... lots of code
```

#### After (FastMCP)
```python
@mcp.tool
async def get_thread_metadata(thread_id: str, ctx: Context) -> str:
    """Get metadata of a Quip thread including its id, type, title and link."""
    # FastMCP handles all the protocol details
    await ctx.info(f"Fetching metadata for thread: {thread_id}")
    # ... business logic only
```

### 3. New Features

#### Middleware for URL Parameters
```python
class QuipConfigMiddleware(Middleware):
    """Extracts Quip configuration from URL parameters."""
    async def on_request(self, context: MiddlewareContext, call_next):
        # Extract quipAccessToken and quipBaseUrl from query params
        # ...
        return await call_next(context)

mcp.add_middleware(QuipConfigMiddleware())
```

#### Resources
```python
@mcp.resource("config://server-info")
def get_server_info() -> dict:
    """Provides server configuration information."""
    return {...}

@mcp.resource("config://quip-settings")
def get_quip_settings() -> dict:
    """Provides current Quip configuration settings."""
    return {...}
```

#### Custom Routes
```python
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for monitoring."""
    return JSONResponse({...})
```

#### Context Integration
```python
@mcp.tool
async def get_thread_content(thread_id: str, ctx: Context) -> str:
    await ctx.info(f"Fetching content for thread: {thread_id}")
    # ... business logic
    await ctx.info(f"Successfully retrieved content")
    # Error handling
    await ctx.error(error_msg)
```

### 4. Comparison Table

| Feature | Original | FastMCP |
|---------|----------|---------|
| Lines of code | 287 | 253 |
| Protocol handling | Manual | Automatic |
| Tool registration | Manual JSON schema | Decorator-based |
| Middleware support | Via Starlette | Native FastMCP |
| Context/logging | Manual | Built-in Context |
| Resources | Not supported | Native support |
| Error handling | Manual try/catch + JSON-RPC | Framework-handled |
| Parameter validation | Manual | Automatic via type hints |
| Health check | Custom route | Custom route |
| SSE support | Via sse-starlette | Built-in |

## Migration Steps

### 1. Install FastMCP
```bash
pip install fastmcp
```

### 2. Replace Starlette App with FastMCP
```python
# Before
app = Starlette(routes=[...])

# After
mcp = FastMCP(name="quip-mcp-server")
```

### 3. Convert Routes to Tools
```python
# Before: Manual route handling
async def mcp_endpoint(request):
    # Parse JSON-RPC
    # Route to handlers
    # Return JSON responses

# After: Simple decorators
@mcp.tool
async def get_thread_metadata(thread_id: str, ctx: Context) -> str:
    # Just business logic
```

### 4. Add Middleware for Custom Logic
```python
class QuipConfigMiddleware(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next):
        # Custom logic here
        return await call_next(context)

mcp.add_middleware(QuipConfigMiddleware())
```

### 5. Add Resources (Optional)
```python
@mcp.resource("config://server-info")
def get_server_info() -> dict:
    return {...}
```

### 6. Run the Server
```python
mcp.run(transport="http", host="0.0.0.0", port=8000)
```

## Testing

### Health Check
```bash
curl http://localhost:8000/health
# {"status":"healthy","service":"quip-mcp-server","framework":"FastMCP"}
```

### MCP Initialize
```bash
curl http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",...}'
```

### Tool Call
The tools are automatically available through the MCP protocol. FastMCP handles:
- Tool listing (`tools/list`)
- Tool invocation (`tools/call`)
- Parameter validation
- Error handling
- Response formatting

## Benefits Summary

### Developer Experience
- ✅ **Less boilerplate**: Focus on business logic, not protocol details
- ✅ **Type safety**: Automatic validation from type hints
- ✅ **Better debugging**: Context-aware logging
- ✅ **Faster development**: Decorators over manual routing

### Maintainability
- ✅ **Cleaner code**: Separation of concerns
- ✅ **Easier testing**: Tools are just async functions
- ✅ **Framework updates**: Protocol changes handled by FastMCP
- ✅ **Documentation**: Auto-generated from docstrings

### Production Ready
- ✅ **Performance**: Built on Starlette/Uvicorn
- ✅ **Scalability**: Async/await throughout
- ✅ **Monitoring**: Built-in middleware hooks
- ✅ **Standards compliant**: Latest MCP protocol

## Running the Servers

### Original Version
```bash
python start_remote_mcp_simple.py
```

### FastMCP Version
```bash
python start_remote_mcp_fastmcp.py
```

Both servers expose the same functionality but with different implementations.

## URL Parameters Support

Both versions support configuration via URL parameters:
```
http://localhost:8000/mcp?quipAccessToken=TOKEN&quipBaseUrl=https://platform.quip-amazon.com
```

The FastMCP version handles this through custom middleware, making it more maintainable and reusable.

## Conclusion

The migration to FastMCP provides:
- **12% less code** with **more features**
- **Better developer experience** through decorators and context
- **Production-ready** framework with active development
- **Future-proof** against MCP protocol changes
- **Native support** for resources, prompts, and middleware

The investment in learning FastMCP pays off quickly through:
- Faster feature development
- Easier maintenance
- Better error handling
- More testable code
- Built-in best practices
