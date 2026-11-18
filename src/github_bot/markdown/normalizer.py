import re
import unicodedata

from pydantic import ValidationError

from github_bot.models import Diagnostic, Priority, Task


def fold(s: str) -> str:
    """casefold + bỏ dấu tiếng Việt, dùng để so khớp tên cột/field (không dùng cho giá trị)."""
    s = unicodedata.normalize("NFD", s.casefold().replace("đ", "d"))
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


_ALIASES = {
    "id": ["ma", "id", "task id", "stt"],
    "title": ["cong viec", "nhiem vu", "chuc nang", "task", "ten task", "title"],
    "assignees": ["phu trach", "nguoi phu trach", "owner", "assignee"],
    "collaborators": ["phoi hop", "reviewer", "ho tro"],
    "description": ["mo ta", "noi dung", "noi dung thuc hien", "description"],
    "acceptance": ["tieu chi nghiem thu", "dau ra", "deliverables", "deliverable", "acceptance criteria"],
    "labels": ["labels", "nhan", "module"],
    "priority": ["uu tien", "priority"],
    "github": ["github"],
    "epic": ["epic"],
    "depends": ["phu thuoc", "depends on", "dependencies"],
    "status": ["trang thai", "status"],  # nhận diện để bỏ qua, schema chưa dùng
}
_FIELD_BY_ALIAS = {a: f for f, names in _ALIASES.items() for a in names}
_PRIORITY: dict[str, Priority] = {
    "high": "high", "cao": "high", "medium": "medium", "trung binh": "medium",
    "low": "low", "thap": "low", "p0": "high", "p1": "medium", "p2": "low",
}  # fmt: skip
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def canon_field(name: str) -> str | None:
    return _FIELD_BY_ALIAS.get(fold(name))


def br_to_newline(s: str) -> str:
    return _BR.sub("\n", s).strip()


def split_list(s: str) -> list[str]:
    """Tách theo , ; <br> xuống dòng; bỏ rỗng và trùng (giữ thứ tự)."""
    parts = (p.strip() for p in re.split(r"[,;\n]|<br\s*/?>", s, flags=re.IGNORECASE))
    return list(dict.fromkeys(p for p in parts if p))


def build_task(
    fields: dict[str, str],
    acceptance: list[str],
    task_id: str,
    source_file: str,
    line: int,
    diags: list[Diagnostic],
) -> Task | None:
    """fields: khóa chuẩn (xem _ALIASES) -> chuỗi thô. Lỗi validate -> diagnostic, trả None."""
    task_id_full, task_id = task_id, task_id[:30]  # ID rác có thể rất dài: cắt khi in
    prio = fields.get("priority", "").strip()
    priority = _PRIORITY.get(fold(prio)) if prio else None
    if prio and priority is None:
        diags.append(Diagnostic(level="warning", code="UNKNOWN_PRIORITY", line=line,
                                message=f"{task_id}: ưu tiên '{prio[:30]}' không nhận diện được, bỏ qua."))  # fmt: skip
    # GitHub login ghi rõ: bỏ '@' đầu, không suy đoán thêm.
    github = [x.removeprefix("@") for x in split_list(fields.get("github", ""))]  # chỉ bỏ MỘT @; `@@x` để resolver chặn
    try:
        return Task(
            id=task_id_full,
            title=fields.get("title", "").strip(),
            description=br_to_newline(fields.get("description", "")),
            assignees=split_list(fields.get("assignees", "")),
            collaborators=split_list(fields.get("collaborators", "")),
            github=github,
            priority=priority,
            labels=split_list(fields.get("labels", "")),
            epic=" ".join(fields.get("epic", "").split()),
            dependencies=[d for d in split_list(fields.get("depends", "")) if d not in ("—", "-", "–")],
            acceptance_criteria=acceptance,
            source_file=source_file,
            source_reference=task_id_full,
        )
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            diags.append(Diagnostic(level="error", code="INVALID_TASK", line=line,
                                    message=f"{task_id}: trường '{loc}' không hợp lệ ({err['msg']})."))  # fmt: skip
        return None
