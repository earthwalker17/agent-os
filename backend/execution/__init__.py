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
from .background import (
    BackgroundRunManager,
    get_default_manager,
    shutdown_default_manager,
)
from .chat_delegation import (
    is_code_delegation,
    handle_code_delegation,
    GENERAL_REJECTION_MESSAGE,
)
from .delegation_intent import (
    looks_like_code_request,
    derive_task_card,
    render_suggestion,
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
    "BackgroundRunManager",
    "get_default_manager",
    "shutdown_default_manager",
    "is_code_delegation",
    "handle_code_delegation",
    "GENERAL_REJECTION_MESSAGE",
    "looks_like_code_request",
    "derive_task_card",
    "render_suggestion",
]
