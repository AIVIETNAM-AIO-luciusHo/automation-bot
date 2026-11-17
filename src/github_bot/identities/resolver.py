import re
import unicodedata
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from github_bot.markdown.loader import LoaderError, read_workspace_file
from github_bot.models import Diagnostic, Task

# GitHub login: 1-39 ký tự chữ/số/'-', không bắt đầu/kết thúc bằng '-', không có '--'.
GITHUB_LOGIN = re.compile(r"^(?!.*--)[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_MEMBER_ID = re.compile(r"^TV\d+$", re.IGNORECASE)  # hai ID chỉ khác hoa/thường -> trùng khóa -> từ chối


class MembersError(Exception):
    pass


class Member(BaseModel, extra="forbid"):
    display_name: str = Field(min_length=1, max_length=100)
    github: str
    aliases: list[str] = []


def _key(s: str) -> str:
    """So khớp CHÍNH XÁC sau NFC + casefold + gộp khoảng trắng. Không bỏ dấu, không fuzzy."""
    return " ".join(unicodedata.normalize("NFC", s).casefold().split())


class Directory:
    def __init__(self, members: dict[str, Member]) -> None:
        self.members = members
        self._names: dict[str, str] = {}
        self._logins: dict[str, str] = {}
        for mid, m in members.items():
            for name in (mid, m.display_name, *m.aliases):
                other = self._names.setdefault(_key(name), mid)
                if other != mid:  # tên/alias/ID dùng chung cho 2 người -> danh bạ mơ hồ, từ chối
                    raise MembersError(f"{name[:40]!r} trùng giữa {other} và {mid}.")
            self._logins[m.github.casefold()] = m.github

    @classmethod
    def from_text(cls, text: str) -> "Directory":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise MembersError("Danh bạ không phải YAML hợp lệ.") from e
        raw = data.get("members") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or not raw:
            raise MembersError("Danh bạ cần khóa `members:` là mapping không rỗng.")
        members: dict[str, Member] = {}
        seen: dict[str, str] = {}
        for mid, body in raw.items():
            if not isinstance(mid, str) or not _MEMBER_ID.match(mid):
                raise MembersError(f"ID thành viên {str(mid)[:20]!r} không hợp lệ (cần dạng TV01).")
            try:
                m = Member.model_validate(body)
            except ValidationError as e:
                fields = ", ".join(str(err["loc"][0]) if err["loc"] else "?" for err in e.errors())
                raise MembersError(f"Thành viên {mid}: trường không hợp lệ ({fields}).") from e
            if not GITHUB_LOGIN.match(m.github):
                raise MembersError(f"Thành viên {mid}: GitHub login không hợp lệ.")
            if m.github.casefold() in seen:
                raise MembersError(f"GitHub login của {mid} trùng với {seen[m.github.casefold()]}.")
            seen[m.github.casefold()] = mid
            members[mid] = m
        return cls(members)

    def resolve_ref(self, ref: str) -> tuple[str | None, tuple[str, str] | None]:
        """Một tham chiếu thô -> (login, None) hoặc (None, (mã lỗi, lý do)). Không đoán."""
        ref = ref.strip()
        shown = repr(ref[:40])
        if ref.startswith("@"):
            login = ref[1:]
            if not GITHUB_LOGIN.match(login):
                return None, ("INVALID_LOGIN", f"GitHub login {shown} sai định dạng.")
            known = self._logins.get(login.casefold())
            if known is None:
                return None, ("UNKNOWN_LOGIN", f"login {shown} không có trong danh bạ (không đoán).")
            return known, None
        mid = self._names.get(_key(ref))
        if mid is not None:
            return self.members[mid].github, None
        if ref.casefold() in self._logins:  # login trần khớp CHÍNH XÁC login của một thành viên
            return self._logins[ref.casefold()], None
        return None, ("UNKNOWN_MEMBER", f"{shown} không có trong danh bạ (không đoán).")


def load_members(path: str | Path, workspace: str | Path) -> Directory:
    try:
        text, _ = read_workspace_file(path, workspace, (".yaml", ".yml"))
    except LoaderError as e:
        raise MembersError(str(e)) from e
    return Directory.from_text(text)


class Resolved(BaseModel):
    task_id: str
    assignees: list[str] = []
    collaborators: list[str] = []
    diagnostics: list[Diagnostic] = []  # error = BLOCK, warning = không chặn

    @property
    def blocked(self) -> bool:
        return any(d.level == "error" for d in self.diagnostics)


def _dedupe(logins: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for x in logins:
        seen.setdefault(x.casefold(), x)
    return list(seen.values())


def resolve_task(task: Task, directory: Directory, assign_collaborators: bool = False) -> Resolved:
    """Phân giải assignees/collaborators/github. Mọi tham chiếu không chắc chắn -> BLOCK toàn task
    (không bao giờ trả tập assignee thiếu). Người phối hợp chỉ thành assignee khi bật cờ rõ ràng."""
    diags: list[Diagnostic] = []

    def block(code: str, msg: str) -> None:
        diags.append(Diagnostic(level="error", code=code, message=f"{task.id}: {msg}"))

    def many(refs: list[str], prefix: str = "") -> list[str]:
        out: list[str] = []
        for ref in refs:
            login, err = directory.resolve_ref(prefix + ref)
            if err:
                block(*err)
            elif login:
                out.append(login)
        return out

    assignees = many(task.assignees)
    collaborators = many(task.collaborators)
    explicit = many(task.github, "@")
    # Login ghi rõ phải khớp tham chiếu thành viên; xung đột -> BLOCK, không chọn bên nào.
    have = {x.casefold() for x in assignees}
    if explicit and have and not {x.casefold() for x in explicit} <= have:
        block("LOGIN_CONFLICT", f"phụ trách {[a[:30] for a in task.assignees]} -> {assignees} "
              f"nhưng trường GitHub ghi {explicit}; không chọn bên nào.")  # fmt: skip
    if diags:
        return Resolved(task_id=task.id, diagnostics=diags)
    if not task.assignees and not task.github:  # không bao giờ tự gán ai
        diags.append(Diagnostic(level="warning", code="NO_ASSIGNEE",
                                message=f"{task.id}: không có người phụ trách, issue sẽ không có assignee."))  # fmt: skip
    final = assignees + explicit + (collaborators if assign_collaborators else [])
    return Resolved(task_id=task.id, assignees=_dedupe(final), collaborators=_dedupe(collaborators),
                    diagnostics=diags)  # fmt: skip
