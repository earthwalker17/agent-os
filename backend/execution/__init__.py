"""Execution layer for Agent OS — project-scoped Coding Agent workspaces."""

from .models import (
    ExecutionWorkspace,
    TaskSpec,
    RunRecord,
    RunStatus,
    ResultSummary,
)
from .manager import (
    get_execution_root,
    get_project_execution_dir,
    init_execution_workspace,
    get_execution_workspace,
    read_task_state,
    update_task_state,
)
from .sandbox import ProjectSandbox, SandboxViolation
from .tool_models import ToolResult
from .tool_runtime import ToolRuntime
from .runner import CodingAgentRunner
from .chat_delegation import (
    is_code_delegation,
    handle_code_delegation,
    GENERAL_REJECTION_MESSAGE,
)

__all__ = [
    "ExecutionWorkspace",
    "TaskSpec",
    "RunRecord",
    "RunStatus",
    "ResultSummary",
    "get_execution_root",
    "get_project_execution_dir",
    "init_execution_workspace",
    "get_execution_workspace",
    "read_task_state",
    "update_task_state",
    "ProjectSandbox",
    "SandboxViolation",
    "ToolResult",
    "ToolRuntime",
    "CodingAgentRunner",
    "is_code_delegation",
    "handle_code_delegation",
    "GENERAL_REJECTION_MESSAGE",
]
