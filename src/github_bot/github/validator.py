"""Kiểm tra chỉ đọc trước khi lập plan: repo, assignee đủ điều kiện, label tồn tại. Fail closed."""

from github_bot.github.client import GitHubClient, GitHubError
from github_bot.identities.resolver import Resolved
from github_bot.models import Diagnostic, Task


def _err(code: str, msg: str) -> Diagnostic:
    return Diagnostic(level="error", code=code, message=msg)


def validate_against_github(
    client: GitHubClient, repo: str, tasks: list[Task], resolved: list[Resolved]
) -> list[Diagnostic]:
    """Trả danh sách BLOCK (rỗng = hợp lệ). Task đã BLOCK ở M2a không bị kiểm lại."""
    owner, _, name = repo.partition("/")
    try:
        info = client.get_repo(owner, name)
        if info is None:
            return [_err("REPO_NOT_FOUND", f"{repo}: không tồn tại hoặc token không có quyền truy cập.")]
        full = info.get("full_name")
        if not isinstance(full, str) or full.casefold() != repo.casefold():
            return [_err("BAD_RESPONSE", f"{repo}: phản hồi repo thiếu hoặc sai full_name.")]
        if info.get("archived") is not False or info.get("disabled") is True:
            return [_err("REPO_READONLY", f"{repo}: repo bị archive/disabled hoặc thiếu trường archived.")]
        if info.get("has_issues") is not True:
            return [_err("ISSUES_DISABLED", f"{repo}: repo tắt Issues hoặc thiếu trường has_issues.")]
    except GitHubError as e:
        return [_err(e.code, f"{repo}: {e.message}")]

    by_id = {t.id: t for t in tasks}
    ok = [r for r in resolved if not r.blocked]
    if any(r.assignees for r in ok):
        perms = info.get("permissions")
        # GitHub âm thầm bỏ assignee nếu người tạo thiếu quyền push/triage -> BLOCK, thiếu trường cũng BLOCK.
        if not isinstance(perms, dict) or not (perms.get("push") is True or perms.get("triage") is True):
            return [_err("NO_ASSIGN_PERMISSION", f"{repo}: token thiếu quyền push/triage nên assignee sẽ bị bỏ im lặng.")]
    diags: list[Diagnostic] = []
    try:
        assignable: dict[str, bool] = {}
        for r in ok:
            for login in r.assignees:
                key = login.casefold()
                if key not in assignable:
                    try:
                        assignable[key] = client.can_assign(owner, name, login)
                    except GitHubError as e:  # dừng ngay; nêu rõ login đang kiểm tra
                        diags.append(_err(e.code, f"{r.task_id}: kiểm tra assignee {login!r} thất bại: {e.message}"))
                        return diags
                if not assignable[key]:
                    diags.append(_err("NOT_ASSIGNABLE", f"{r.task_id}: {login!r} không thể được assign trong {repo}."))
        if any(by_id[r.task_id].labels for r in ok):
            existing = client.label_names(owner, name)
            for r in ok:
                for label in by_id[r.task_id].labels:
                    if label.casefold() not in existing:  # ensure_labels_exist=false: không tự tạo
                        diags.append(_err("LABEL_MISSING", f"{r.task_id}: label {label[:50]!r} không tồn tại trong {repo}."))
    except GitHubError as e:  # lỗi hệ thống giữa chừng (mạng, 5xx, 401/403, rate limit): dừng, BLOCK
        diags.append(_err(e.code, f"{repo}: {e.message} Kết quả kiểm tra không đầy đủ."))
    return diags
