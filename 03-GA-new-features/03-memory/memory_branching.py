import logging
from datetime import datetime
from strands.hooks import AgentInitializedEvent, HookProvider, HookRegistry, MessageAddedEvent


import os
region = os.getenv('AWS_REGION', 'us-west-2')
MODEL_ID = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("agentcore-memory")

from bedrock_agentcore.memory import MemoryClient
client = MemoryClient(region_name=region)
memory_name = "TravelAgent_STM_test1"
memory_id = None

from botocore.exceptions import ClientError

try:
    print("Creating Memory...")
    memory_name = memory_name

    # Create the memory resource
    memory = client.create_memory_and_wait(
        name=memory_name,                       # Unique name for this memory store
        description="Travel Agent STM",         # Human-readable description
        strategies=[],                          # No special memory strategies for short-term memory
        event_expiry_days=7,                    # Memories expire after 7 days
        max_wait=300,                           # Maximum time to wait for memory creation (5 minutes)
        poll_interval=10                        # Check status every 10 seconds
    )

    # Extract and print the memory ID
    memory_id = memory['id']
    print(f"Memory created successfully with ID: {memory_id}")
except ClientError as e:
    if e.response['Error']['Code'] == 'ValidationException' and "already exists" in str(e):
        # If memory already exists, retrieve its ID
        memories = client.list_memories()
        memory_id = next((m['id'] for m in memories if m['id'].startswith(memory_name)), None)
        logger.info(f"Memory already exists. Using existing memory ID: {memory_id}")
except Exception as e:
    # Handle any errors during memory creation
    print(f"❌ ERROR: {e}")
    import traceback
    traceback.print_exc()

    # Cleanup on error - delete the memory if it was partially created
    if memory_id:
        try:
            client.delete_memory_and_wait(memory_id=memory_id)
            logger.info(f"Cleaned up memory: {memory_id}")
        except Exception as cleanup_error:
            logger.info(f"Failed to clean up memory: {cleanup_error}")

from bedrock_agentcore.memory.constants import ConversationalMessage, MessageRole
from bedrock_agentcore.memory import MemorySessionManager
class ShortTermMemoryHook(HookProvider):
    def __init__(self, memory_id: str, region_name: str = "us-west-2", branch_name: str = "main"):
        """Initialize the hook with a MemorySessionManager.

        Args:
            memory_id: The AgentCore Memory ID
            region_name: AWS region for the memory service
            branch_name: Branch name for this agent's memory (default: "main")
        """
        self.memory_manager = MemorySessionManager(
            memory_id=memory_id,
            region_name=region_name
        )
        self.memory_id = memory_id
        self.branch_name = branch_name
        self._sessions = {}  # Cache session objects per actor/session combo
        self._branch_initialized = False  # Track if branch has been created

    def _get_or_create_session(self, actor_id: str, session_id: str):
        """Get or create a MemorySession for the given actor/session.

        Args:
            actor_id: The actor identifier
            session_id: The session identifier

        Returns:
            MemorySession object
        """
        key = f"{actor_id}:{session_id}"
        if key not in self._sessions:
            self._sessions[key] = self.memory_manager.create_memory_session(
                actor_id=actor_id,
                session_id=session_id
            )
        return self._sessions[key]

    def _initialize_branch(self, actor_id: str, session_id: str):
        """Initialize a branch if it doesn't exist and this is not the main branch.

        Args:
            actor_id: The actor identifier
            session_id: The session identifier
        """
        if self._branch_initialized or self.branch_name == "main":
            return

        try:
            memory_session = self._get_or_create_session(actor_id, session_id)

            # Check if branch already exists
            branches = memory_session.list_branches()
            branch_exists = any(b.name == self.branch_name for b in branches)

            if not branch_exists:
                # Get the last event from main branch to fork from
                main_events = memory_session.list_events(branch_name="main")
                if main_events:
                    last_event = main_events[-1]
                    # Create the branch with an initial message
                    memory_session.fork_conversation(
                        root_event_id=last_event.eventId,
                        branch_name=self.branch_name,
                        messages=[
                            ConversationalMessage(f"Starting {self.branch_name} branch", MessageRole.ASSISTANT)
                        ]
                    )
                    logger.info(f"✅ Created branch: {self.branch_name}")

            self._branch_initialized = True

        except Exception as e:
            logger.error(f"Failed to initialize branch {self.branch_name}: {e}", exc_info=True)

    def on_agent_initialized(self, event: AgentInitializedEvent):
        """Load recent conversation history when agent starts"""
        try:
            # Get session info from agent state
            actor_id = event.agent.state.get("actor_id")
            session_id = event.agent.state.get("session_id")

            if not actor_id or not session_id:
                logger.warning("Missing actor_id or session_id in agent state")
                return

            # Get the memory session
            memory_session = self._get_or_create_session(actor_id, session_id)

            # For non-main branches, initialize if there are events in main branch
            if self.branch_name != "main":
                try:
                    main_events = memory_session.list_events(branch_name="main")
                    if len(main_events) > 0:
                        self._initialize_branch(actor_id, session_id)
                except Exception as e:
                    # Main branch might not exist yet on first call
                    logger.info(f"Main branch not found yet, will initialize {self.branch_name} branch later: {e}")

            # Check if the branch exists before trying to get turns
            branches = memory_session.list_branches()
            branch_exists = any(b.name == self.branch_name for b in branches)
            
            recent_turns = []
            if branch_exists:
                # Only fetch turns if branch exists
                recent_turns = memory_session.get_last_k_turns(
                    k=5,
                    branch_name=self.branch_name
                )
            else:
                logger.info(f"Branch '{self.branch_name}' does not exist yet, skipping turn retrieval")

            if len(recent_turns) > 0:
                # Format conversation history for context
                context_messages = []
                for turn in recent_turns:
                    for message in turn:
                        role = message.content.get('role', 'unknown').lower()
                        text = message.content.get('content', {}).get('text', '')
                        if text:
                            context_messages.append(f"{role.title()}: {text}")

                if context_messages:
                    context = "\n".join(context_messages)
                    logger.info(f"Loaded context from branch '{self.branch_name}' ({len(context_messages)} messages)")

                    # Add context to agent's system prompt
                    event.agent.system_prompt += (
                        f"\n\nRecent conversation history (from {self.branch_name}):\n{context}\n\n"
                        "Continue the conversation naturally based on this context."
                    )

                    logger.info(f"✅ Loaded {len(recent_turns)} recent conversation turns from branch '{self.branch_name}'")
            else:
                logger.info(f"No previous conversation history found in branch '{self.branch_name}'")

        except Exception as e:
            logger.error(f"Failed to load conversation history: {e}", exc_info=True)

    def on_message_added(self, event: MessageAddedEvent):
        """Store conversation turns in memory on the appropriate branch"""
        try:
            # Get session info from agent state
            actor_id = event.agent.state.get("actor_id")
            session_id = event.agent.state.get("session_id")

            if not actor_id or not session_id:
                logger.warning("Missing actor_id or session_id in agent state")
                return

            # Get the memory session
            memory_session = self._get_or_create_session(actor_id, session_id)

            # Get the last message
            messages = event.agent.messages
            if not messages:
                return

            last_message = messages[-1]
            role_str = last_message.get("role", "").upper()
            content_text = last_message.get("content", [{}])[0].get("text", "")

            if not content_text:
                logger.debug("Skipping empty message")
                return

            # Map role string to MessageRole enum
            role_mapping = {
                "USER": MessageRole.USER,
                "ASSISTANT": MessageRole.ASSISTANT,
                "TOOL": MessageRole.TOOL,
            }
            message_role = role_mapping.get(role_str, MessageRole.USER)

            # Store the message on the appropriate branch
            if self.branch_name == "main":
                # Main branch - just add turns normally
                memory_session.add_turns(
                    messages=[ConversationalMessage(content_text, message_role)]
                )
            else:
                # Non-main branch - need to append to existing branch
                # Initialize branch if it doesn't exist
                if not self._branch_initialized:
                    self._initialize_branch(actor_id, session_id)

                # Get the latest event from this branch
                branch_events = memory_session.list_events(branch_name=self.branch_name)
                if branch_events:
                    # Add to existing branch by specifying branch name (without rootEventId)
                    memory_session.add_turns(
                        messages=[ConversationalMessage(content_text, message_role)],
                        branch={"name": self.branch_name}
                    )
                else:
                    # This shouldn't happen if _initialize_branch worked, but handle it
                    logger.warning(f"Branch {self.branch_name} not found after initialization")
                    self._initialize_branch(actor_id, session_id)

            logger.debug(f"✅ Stored message in branch '{self.branch_name}': {role_str}")

        except Exception as e:
            logger.error(f"Failed to store message: {e}", exc_info=True)

    def create_branch(self, actor_id: str, session_id: str,
                      root_event_id: str, branch_name: str,
                      messages: list):
        """Create a new conversation branch.

        Args:
            actor_id: The actor identifier
            session_id: The session identifier
            root_event_id: Event ID to branch from
            branch_name: Name for the new branch
            messages: List of ConversationalMessage objects to add to the branch
        """
        memory_session = self._get_or_create_session(actor_id, session_id)
        return memory_session.fork_conversation(
            root_event_id=root_event_id,
            branch_name=branch_name,
            messages=messages
        )

    def list_branches(self, actor_id: str, session_id: str):
        """List all branches for a session.

        Args:
            actor_id: The actor identifier
            session_id: The session identifier

        Returns:
            List of branch information
        """
        memory_session = self._get_or_create_session(actor_id, session_id)
        return memory_session.list_branches()

    def get_session(self, actor_id: str, session_id: str):
        """Get the memory session object for direct access.

        Args:
            actor_id: The actor identifier
            session_id: The session identifier

        Returns:
            MemorySession object
        """
        return self._get_or_create_session(actor_id, session_id)

    def register_hooks(self, registry: HookRegistry) -> None:
        """Register memory hooks with the registry.

        Args:
            registry: The HookRegistry to register callbacks with
        """
        registry.add_callback(MessageAddedEvent, self.on_message_added)
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)
        
# Import the necessary components
from strands import Agent, tool
# Create unique actor IDs for each specialized agent but share the session ID
actor_id = f"travel-user-{datetime.now().strftime('%Y%m%d%H%M%S')}"
session_id = f"travel-session-{datetime.now().strftime('%Y%m%d%H%M%S')}"
namespace = f"travel/{actor_id}/preferences"
# System prompt for the hotel booking specialist
HOTEL_BOOKING_PROMPT = f"""You are a hotel booking assistant. Help customers find hotels, make reservations, and answer questions about accommodations and amenities. 
Provide clear information about availability, pricing, and booking procedures in a friendly, helpful manner."""

# System prompt for the flight booking specialist
FLIGHT_BOOKING_PROMPT = f"""You are a flight booking assistant. Help customers find flights, make reservations, and answer questions about airlines, routes, and travel policies. 
Provide clear information about flight availability, pricing, schedules, and booking procedures in a friendly, helpful manner."""

flight_memory_hooks = None
hotel_memory_hooks = None


# Implementing Agents with Memory Branching¶

# Each specialized agent is configured with:

#     A unique branch_name for isolated memory context
#     The same memory_id and session_id for shared session management
#     A ShortTermMemoryHook that handles branch-specific operations

# Key Implementation Details:

#     flight_booking_agent() uses branch: flight_agent_memory
#     hotel_booking_agent() uses branch: hotel_agent_memory


def flight_booking_agent() -> Agent:

    global flight_memory_hooks
    try:
        if flight_memory_hooks is None:
            # Create hook with branch name "flight_agent_memory"
            flight_memory_hooks = ShortTermMemoryHook(
                memory_id=memory_id,
                region_name=region,
                branch_name="flight_agent_memory"
            )

        flight_agent = Agent(
            hooks=[flight_memory_hooks],
            model=MODEL_ID,
            system_prompt=FLIGHT_BOOKING_PROMPT,
            state={"actor_id": actor_id, "session_id": session_id}
        )

        
        return flight_agent
    except Exception as e:
        return f"Error in flight booking assistant: {str(e)}"

def hotel_booking_agent() -> Agent:

    global hotel_memory_hooks
    try:
        if hotel_memory_hooks is None:
            # Create hook with branch name "hotel_agent_memory"
            hotel_memory_hooks = ShortTermMemoryHook(
                memory_id=memory_id,
                region_name=region,
                branch_name="hotel_agent_memory"
            )

        hotel_booking_agent = Agent(
            hooks=[hotel_memory_hooks],
            model=MODEL_ID,
            system_prompt=HOTEL_BOOKING_PROMPT,
            state={"actor_id": actor_id, "session_id": session_id}
        )

        return hotel_booking_agent
    except Exception as e:
        return f"Error in hotel booking assistant: {str(e)}"

# System prompt for the coordinator agent
TRAVEL_AGENT_SYSTEM_PROMPT = """
You are a comprehensive travel planning assistant that coordinates between specialized tools:
- For flight-related queries (bookings, schedules, airlines, routes) → Use the flight_booking_agent
- For hotel-related queries (accommodations, amenities, reservations) → Use the hotel_booking_agent
- For complete travel packages → Use both tools as needed to provide comprehensive information
- For general travel advice or simple travel questions → Answer directly

Each agent will have its own memory in case the user asks about historic data.
When handling complex travel requests, coordinate information from both tools to create a cohesive travel plan.
Provide clear organization when presenting information from multiple sources. \
Ask max two questions per turn. Keep the messages short, don't overwhelm the customer.
"""


def travel_booking_agent() -> Agent:

    agent_memory_hooks = ShortTermMemoryHook(
                    memory_id=memory_id,
                    region_name=region,
                )
    travel_agent = Agent(
    system_prompt=TRAVEL_AGENT_SYSTEM_PROMPT,
    hooks=[agent_memory_hooks],
    model=MODEL_ID,
    state={
        "actor_id": actor_id,
        "session_id": session_id
        }
    )

    return travel_agent

import logging
from strands import Agent
from strands.multiagent import GraphBuilder

# Enable debug logs and print them to stderr
logging.getLogger("strands.multiagent").setLevel(logging.DEBUG)
logging.basicConfig(
    format="%(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()]
)

# Build the Strands Agent Graph
# This graph structure enables parallel execution of specialized agents
# Memory branching ensures safe concurrent access without conflicts
builder = GraphBuilder()

# Add nodes - each agent with its own memory branch
builder.add_node(travel_booking_agent(), "travel_agent")           # Uses 'main' branch
builder.add_node(flight_booking_agent(), "flight_booking_agent")   # Uses 'flight_agent_memory' branch
builder.add_node(hotel_booking_agent(), "hotel_booking_agent")     # Uses 'hotel_agent_memory' branch

# Add edges - define which agents the coordinator can delegate to
# The graph can execute flight and hotel agents in parallel when both are needed
builder.add_edge("travel_agent", "flight_booking_agent")
builder.add_edge("travel_agent", "hotel_booking_agent")

# Set entry point - the coordinator agent receives user input first
builder.set_entry_point("travel_agent")

# Configure execution limits for safety
builder.set_execution_timeout(600)   # 10 minute timeout

# Build the graph - ready for parallel execution with isolated memory contexts
graph = builder.build()


response = graph("Hello, I would like to book a trip from LA to Madrid. From July 1 to August 2.")


print("\n=== Viewing Memory Branches ===")

if flight_memory_hooks or hotel_memory_hooks:
    # Get any memory session to list branches (they all point to the same session)
    hook = flight_memory_hooks if flight_memory_hooks else hotel_memory_hooks
    if hook:
        memory_session = hook.get_session(actor_id, session_id)

        # List all branches in the session
        branches = memory_session.list_branches()
        print(f"\n📊 Session has {len(branches)} branches total:")
        for branch in branches:
            print(f"  - Branch: {branch.name}")
            print(f"    └─ Events: {len(memory_session.list_events(branch_name=branch.name))}")
            print(f"    └─ Created: {branch.created}")

        print("\n💡 Each branch represents a different agent's memory:")
        print("  • 'main' = Travel coordinator conversations")
        print("  • 'flight_agent_memory' = Flight assistant conversations")
        print("  • 'hotel_agent_memory' = Hotel assistant conversations")
        
        
print("\n=== Accessing Branch-Specific Events ===")

if flight_memory_hooks or hotel_memory_hooks:
    hook = flight_memory_hooks if flight_memory_hooks else hotel_memory_hooks
    if hook:
        memory_session = hook.get_session(actor_id, session_id)

        # Get events from the main branch (coordinator)
        main_events = memory_session.list_events(branch_name="main")
        print(f"\n🌳 Main Branch - Coordinator ({len(main_events)} events):")
        if main_events:
            for event in main_events[-3:]:  # Show last 3 events
                for payload in event.payload:
                    if 'conversational' in payload:
                        role = payload['conversational']['role']
                        text = payload['conversational']['content']['text']
                        print(f"  {role}: {text[:100]}...")
        else:
            print("  No events found in main branch")

        # Get events from the flight agent branch
        try:
            flight_branch_events = memory_session.list_events(branch_name="flight_agent_memory")
            print(f"\n✈️  Flight Agent Branch ({len(flight_branch_events)} events):")
            if flight_branch_events:
                print("All flight-related conversations are stored here:")
                for event in flight_branch_events[-3:]:  # Show last 3 events
                    for payload in event.payload:
                        if 'conversational' in payload:
                            role = payload['conversational']['role']
                            text = payload['conversational']['content']['text']
                            print(f"  {role}: {text[:100]}...")
            else:
                print("  No events found - flight assistant wasn't called yet")
        except Exception as e:
            print(f"  Flight branch not created yet: {e}")

        # Get events from the hotel agent branch
        try:
            hotel_branch_events = memory_session.list_events(branch_name="hotel_agent_memory")
            print(f"\n🏨 Hotel Agent Branch ({len(hotel_branch_events)} events):")
            if hotel_branch_events:
                print("All hotel-related conversations are stored here:")
                for event in hotel_branch_events[-3:]:  # Show last 3 events
                    for payload in event.payload:
                        if 'conversational' in payload:
                            role = payload['conversational']['role']
                            text = payload['conversational']['content']['text']
                            print(f"  {role}: {text[:100]}...")
            else:
                print("  No events found - hotel assistant wasn't called yet")
        except Exception as e:
            print(f"  Hotel branch not created yet: {e}")