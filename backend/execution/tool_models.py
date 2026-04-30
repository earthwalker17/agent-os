"""Request/response models for the execution tool runtime."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    success: bool
    tool_name: str
    output: str = ""
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListFilesRequest(BaseModel):
    path: str = "."


class ReadFileRequest(BaseModel):
    path: str


class WriteFileRequest(BaseModel):
    path: str
    content: str


class AppendFileRequest(BaseModel):
    path: str
    content: str


class SearchFilesRequest(BaseModel):
    query: str
    path: str = "."


class RunShellRequest(BaseModel):
    command: str
    timeout_seconds: int = 30
