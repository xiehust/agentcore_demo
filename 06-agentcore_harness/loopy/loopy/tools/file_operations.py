"""File tool for viewing and modifying files.

Uses the container manager's FileIO to support both local Python I/O and CinC shell-based I/O.
Supports: view (read file/dir), create (write file), str_replace (edit), insert (add lines).
"""

import logging
from typing import Optional

from strands import tool

from loopy.abstract import FileIO, LoopyContainerManager

logger = logging.getLogger(__name__)


def create_file_operations_tool(container_manager: LoopyContainerManager):
    io = container_manager.file_io

    @tool
    def file_operations(
        command: str,
        path: str,
        old_str: Optional[str] = None,
        new_str: Optional[str] = None,
        file_text: Optional[str] = None,
        insert_line: Optional[int] = None,
        view_range: Optional[list[int]] = None,
    ) -> str:
        """Text editor tool for viewing and modifying files.

        Args:
            command: The command to execute ("view", "str_replace", "create", "insert")
            path: Path to the file or directory
            old_str: Text to replace (for str_replace command)
            new_str: Replacement text (for str_replace and insert commands)
            file_text: Content for new file (for create command)
            insert_line: Line number to insert after (for insert command)
            view_range: [start_line, end_line] for viewing specific lines (for view command)

        Returns:
            Result of the operation
        """
        try:
            if command == "view":
                return _handle_view(io, path, view_range)
            elif command == "str_replace":
                if old_str is None or new_str is None:
                    return "Error: str_replace requires both old_str and new_str parameters"
                return _handle_str_replace(io, path, old_str, new_str)
            elif command == "create":
                if file_text is None:
                    return "Error: create requires file_text parameter"
                return _handle_create(io, path, file_text)
            elif command == "insert":
                if new_str is None or insert_line is None:
                    return "Error: insert requires both new_str and insert_line parameters"
                return _handle_insert(io, path, new_str, insert_line)
            else:
                return f"Error: Unknown command '{command}'"
        except Exception as e:
            return f"Error: {e}"

    return file_operations


def _handle_view(io: FileIO, path: str, view_range: Optional[list[int]] = None) -> str:
    if not io.exists(path):
        return f"Error: Path '{path}' does not exist"
    if io.is_dir(path):
        return io.listdir(path)
    content = io.read(path)
    lines = content.splitlines()
    if view_range:
        start_line, end_line = view_range
        start_idx = max(0, start_line - 1) if start_line > 0 else 0
        end_idx = len(lines) if end_line == -1 else min(len(lines), end_line)
        lines = lines[start_idx:end_idx]
        start_line_num = start_idx + 1
    else:
        start_line_num = 1
    return "\n".join(f"{start_line_num + i}: {line}" for i, line in enumerate(lines))


def _handle_str_replace(io: FileIO, path: str, old_str: str, new_str: str) -> str:
    if not io.exists(path):
        return f"Error: File '{path}' does not exist"
    content = io.read(path)
    if old_str not in content:
        return "Error: Text not found in file"
    count = content.count(old_str)
    if count > 1:
        return f"Error: Text appears {count} times in file. Please be more specific."
    io.write(path, content.replace(old_str, new_str, 1))
    return f"Successfully replaced text in '{path}'"


def _handle_create(io: FileIO, path: str, file_text: str) -> str:
    io.mkdir_parents(path)
    io.write(path, file_text)
    return f"Successfully created file '{path}'"


def _handle_insert(io: FileIO, path: str, new_str: str, insert_line: int) -> str:
    if not io.exists(path):
        return f"Error: File '{path}' does not exist"
    content = io.read(path)
    lines = content.splitlines(True)
    if insert_line == 0:
        lines.insert(0, new_str + "\n")
    elif insert_line >= len(lines):
        lines.append(new_str + "\n")
    else:
        lines.insert(insert_line, new_str + "\n")
    io.write(path, "".join(lines))
    return f"Successfully inserted text in '{path}' at line {insert_line + 1}"
