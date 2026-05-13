"""Shell tool — routes through container manager for local or CinC execution."""

from strands import tool

from loopy.abstract import LoopyContainerManager


def create_shell_tool(container_manager: LoopyContainerManager):
    @tool
    def shell(command: str, timeout: int = 300) -> dict:
        """Execute a bash command and return the results.

        Args:
            command: The bash command to execute
            timeout: Timeout in seconds (default: 300)

        Returns:
            Dict with stdout, stderr, and exit_code
        """
        return container_manager.run(command, timeout)

    return shell
