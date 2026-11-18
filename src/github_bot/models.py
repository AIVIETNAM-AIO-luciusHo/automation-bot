from typing import Literal

from pydantic import BaseModel, Field

Priority = Literal["low", "medium", "high"]


class Task(BaseModel):
    """Task chuẩn hóa (§3.4). assignees/collaborators là tham chiếu THÔ như người viết ghi
    (TV02, tên, @login); resolver (M2) mới ánh xạ sang GitHub login. github = login ghi rõ."""

    id: str = Field(pattern=r"^[A-Za-z]+-?\d+$", max_length=64)
    title: str = Field(min_length=1, max_length=256)
    description: str = ""
    assignees: list[str] = []
    collaborators: list[str] = []
    github: list[str] = []
    priority: Priority | None = None
    labels: list[str] = []
    acceptance_criteria: list[str] = []
    source_file: str
    source_reference: str


class Diagnostic(BaseModel):
    level: Literal["error", "warning"]
    code: str
    message: str
    line: int | None = None
    task_id: str | None = None  # None = lỗi toàn cục (repo/mạng), ảnh hưởng mọi task


class ParseResult(BaseModel):
    tasks: list[Task] = []
    diagnostics: list[Diagnostic] = []

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "error"]
