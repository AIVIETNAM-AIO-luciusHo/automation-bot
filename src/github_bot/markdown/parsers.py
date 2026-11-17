import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from github_bot.markdown.normalizer import (
    br_to_newline,
    build_task,
    canon_field,
    fold,
    split_list,
)
from github_bot.models import Diagnostic, ParseResult, Task

_TASK_HEADING = re.compile(r"^(TASK-\d+)\s*:\s*(\S.*)$")
_ID = re.compile(r"^[A-Za-z]+-?\d+$")
_SEP_CELL = re.compile(r"^:?-+:?$")
_TASKISH = re.compile(r"^TASK\s*[-:_#\d]", re.IGNORECASE)  # không khớp TASKS, "Task list"
_BULLET_KV = re.compile(r"^[-*]\s+([^:]+?)\s*:\s*(.*)$")
_BULLET = re.compile(r"^[-*]\s+(?:\[[ xX]\]\s*)?(\S.*)$")
_DESC = {"mo ta", "description"}
_AC = {"tieu chi nghiem thu", "acceptance criteria"}
_META_FIELDS = {"assignees", "collaborators", "labels", "github", "priority"}

Found = list[tuple[int, Task]]


def _scan(text: str) -> tuple[list[tuple[int, str]], int | None]:
    """([(số dòng từ 1, nội dung)], dòng mở fence chưa đóng). Bỏ nội dung trong code fence
    (dữ liệu không đáng tin). Fence chỉ đóng bằng đúng ký tự đã mở."""
    out: list[tuple[int, str]] = []
    marker = ""
    opened = 0
    for n, line in enumerate(text.splitlines(), 1):
        st = line.lstrip()
        if st.startswith(("```", "~~~")):
            if not marker:
                marker, opened = st[0], n
            elif st[0] == marker:
                marker = ""
        elif not marker:
            out.append((n, line))
    return out, (opened if marker else None)


def _lines(text: str) -> list[tuple[int, str]]:
    return _scan(text)[0]


@dataclass
class _Section:
    id: str
    line: int
    fields: dict[str, str]
    desc: list[str] = field(default_factory=list)
    ac: list[str] = field(default_factory=list)


def parse_headings(text: str, source_file: str) -> tuple[Found, list[Diagnostic]]:
    found: Found = []
    diags: list[Diagnostic] = []
    cur: _Section | None = None
    mode = ""

    def close() -> None:
        if cur:
            cur.fields["description"] = "\n".join(cur.desc).strip()
            task = build_task(cur.fields, cur.ac, cur.id, source_file, cur.line, diags)
            if task:
                found.append((cur.line, task))

    for n, line in _lines(text):
        wrong = re.match(r"^(#{1,6})\s*(.+?)\s*$", line)
        if wrong and len(wrong.group(1)) != 2 and _TASKISH.match(wrong.group(2)):
            diags.append(Diagnostic(level="error", code="WRONG_HEADING_LEVEL", line=n,
                                    message="Heading task phải là cấp 2 ('## TASK-<số>: <tên>')."))  # fmt: skip
        m = re.match(r"^##(?!#)\s*(.+?)\s*$", line)
        if m:
            close()
            cur = None
            head = m.group(1)
            if _TASKISH.match(head):
                t = _TASK_HEADING.match(head)
                if t and line[2:3].isspace():
                    cur = _Section(t.group(1), n, {"title": t.group(2).strip()})
                    mode = "meta"
                else:
                    diags.append(Diagnostic(level="error", code="MALFORMED_ID", line=n,
                                            message="Heading task phải có dạng '## TASK-<số>: <tên>'."))  # fmt: skip
            continue
        if cur is None:
            continue
        h3 = re.match(r"^###(?!#)\s+(.+?)\s*$", line)
        if h3:
            key = fold(h3.group(1))
            mode = "desc" if key in _DESC else "ac" if key in _AC else "other"
        elif mode == "meta":
            kv = _BULLET_KV.match(line.strip())
            if kv:
                name = canon_field(kv.group(1))
                if name is None:
                    diags.append(Diagnostic(level="warning", code="UNKNOWN_FIELD", line=n,
                                            message=f"{cur.id}: trường '{kv.group(1)[:30]}' không nhận diện, bỏ qua."))  # fmt: skip
                elif name in _META_FIELDS:
                    cur.fields[name] = kv.group(2)
        elif mode == "desc":
            cur.desc.append(line)
        elif mode == "ac":
            b = _BULLET.match(line.strip())
            if b:
                cur.ac.append(b.group(1).strip())
    close()
    return found, diags


def split_row(line: str) -> list[str]:
    """Tách ô bảng; `\\|` và `|` trong inline code không tách ô."""
    s = line.strip()
    s = s.removeprefix("|")
    cells: list[str] = []
    cur: list[str] = []
    in_code = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and s[i + 1 : i + 2] == "|":
            cur.append("|")
            i += 2
            continue
        if c == "`":
            in_code = not in_code
        if c == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = "".join(cur).strip()
    if tail or not s.endswith("|"):
        cells.append(tail)
    return cells


def _blocks(text: str) -> Iterator[list[tuple[int, str]]]:
    """Các cụm dòng liên tiếp có chứa '|' (bảng có hoặc không có '|' đầu dòng)."""
    block: list[tuple[int, str]] = []
    for n, line in _lines(text):
        if "|" in line and (not block or block[-1][0] == n - 1):
            block.append((n, line))
            continue
        if block:
            yield block
        block = [(n, line)] if "|" in line else []
    if block:
        yield block


def parse_tables(text: str, source_file: str) -> tuple[Found, list[Diagnostic]]:
    found: Found = []
    diags: list[Diagnostic] = []
    width = 0  # độ rộng bảng task gần nhất, để phát hiện dòng mồ côi sau dòng trống
    for block in _blocks(text):
        start = block[0][0]
        header = split_row(block[0][1])
        sep_ok = len(block) > 1 and all(_SEP_CELL.match(c) for c in split_row(block[1][1]))
        cols: dict[str, int] = {}
        for i, name in enumerate(header):
            f = canon_field(name)
            if f and f not in cols:
                cols[f] = i
        if "id" not in cols and "title" not in cols:
            if not sep_ok and width and any(_ID.match(c) for c in header):
                diags.append(Diagnostic(level="warning", code="ORPHAN_TABLE_ROW", line=start,
                                        message="Dòng bảng nằm sau dòng trống, không thuộc bảng task nào; không gộp vào bảng trước."))  # fmt: skip
            elif sep_ok:
                diags.append(Diagnostic(level="warning", code="NOT_TASK_TABLE", line=start,
                                        message="Bảng không có cột mã/tên công việc, bỏ qua."))  # fmt: skip
            continue
        if not sep_ok:
            diags.append(Diagnostic(level="error", code="NOT_STANDARD_TABLE", line=start,
                                    message="Bảng thiếu hoặc sai dòng phân cách (ví dụ |---|---|); hãy chuẩn hóa bảng."))  # fmt: skip
            continue
        width = len(header)
        missing = [f for f in ("id", "title") if f not in cols]
        if missing:
            diags.append(Diagnostic(level="error", code="MISSING_COLUMN", line=start,
                                    message=f"Bảng thiếu cột bắt buộc: {', '.join(missing)}."))  # fmt: skip
            continue
        for n, row in block[2:]:
            cells = split_row(row)
            if len(cells) != len(header):
                diags.append(Diagnostic(level="error", code="ROW_MISMATCH", line=n,
                                        message=f"Dòng có {len(cells)} ô, tiêu đề có {len(header)}; hãy chuẩn hóa bảng."))  # fmt: skip
                continue
            tid = cells[cols["id"]]
            if not tid:
                diags.append(Diagnostic(level="error", code="MISSING_ID", line=n,
                                        message="Dòng thiếu mã task ổn định."))  # fmt: skip
                continue
            if not _ID.match(tid):
                diags.append(Diagnostic(level="error", code="MALFORMED_ID", line=n,
                                        message=f"Mã task '{tid[:30]}' không hợp lệ (cần dạng T01 / TASK-001; số thứ tự thuần không ổn định)."))  # fmt: skip
                continue
            fields = {f: cells[i] for f, i in cols.items() if f != "acceptance"}
            ac = split_list(cells[cols["acceptance"]]) if "acceptance" in cols else []
            fields["description"] = br_to_newline(fields.get("description", ""))
            task = build_task(fields, ac, tid, source_file, n, diags)
            if task:
                found.append((n, task))
    return found, diags


def parse_markdown(text: str, source_file: str) -> ParseResult:
    h, hd = parse_headings(text, source_file)
    t, td = parse_tables(text, source_file)
    diags = hd + td
    unclosed = _scan(text)[1]
    if unclosed:
        diags.append(Diagnostic(level="error", code="UNCLOSED_FENCE", line=unclosed,
                                message="Code fence không đóng; phần sau bị bỏ qua."))  # fmt: skip
    seen: dict[str, int] = {}
    tasks: list[Task] = []
    for line, task in sorted(h + t, key=lambda x: x[0]):
        key = task.id.upper()  # fail closed: T01 và t01 là cùng một mã
        if key in seen:
            diags.append(Diagnostic(level="error", code="DUPLICATE_ID", line=line,
                                    message=f"Mã {task.id} trùng (không phân biệt hoa/thường) với dòng {seen[key]}."))  # fmt: skip
            continue
        seen[key] = line
        tasks.append(task)
    if not tasks:
        diags.append(Diagnostic(level="error", code="NO_TASKS",
                                message="Không tìm thấy task nào trong file."))  # fmt: skip
    diags.sort(key=lambda d: d.line or 0)
    return ParseResult(tasks=tasks, diagnostics=diags)
