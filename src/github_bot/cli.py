import argparse
import json
import re
from pathlib import Path
from string import punctuation

import httpx

from github_bot import __version__
from github_bot.execution.dependency_pr import dependency_pr_apply
from github_bot.execution.issue_executor import ApplyRejected, apply_plan
from github_bot.github.client import GitHubClient, GitHubError
from github_bot.github.validator import validate_against_github
from github_bot.identities.resolver import MembersError, load_members, resolve_task
from github_bot.markdown.loader import LoaderError, load_markdown
from github_bot.markdown.parsers import parse_markdown
from github_bot.models import Diagnostic
from github_bot.planning.dependency_pr import dependency_pr_plan
from github_bot.planning.planner import PlanError, build_plan, write_preview

# điều khiển C0/C1 + bidi/zero-width (U+200B-200F, 202A-202E, 2066-2069) có thể đánh lừa terminal
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def _show(msg: str) -> str:
    """In thông báo an toàn: ký tự điều khiển (ESC, CR...) từ dữ liệu người dùng bị thoát, không tới terminal."""
    return _CTRL.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 256 else f"\\u{ord(m.group()):04x}", msg)


def _validate(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    """Mặc định offline. Chỉ khi --check-github mới gọi GitHub (chỉ GET). Không ghi gì."""
    if args.check_github and not args.repo:
        print("LỖI: --check-github cần --repo OWNER/REPO.")
        return 2
    if args.check_github and args.members is None:
        print("LỖI: --check-github cần --members (để biết login cần kiểm tra).")
        return 2
    try:
        text, source = load_markdown(args.input, args.workspace)
    except LoaderError as e:
        print(_show(f"LỖI: {e}"))
        return 2
    directory = None
    if args.members is not None:
        try:
            directory = load_members(args.members, args.workspace)
        except MembersError as e:
            print(_show(f"LỖI danh bạ: {e}"))
            return 2
    result = parse_markdown(text, source)
    for d in result.diagnostics:
        print(_show(f"{d.level.upper()} dòng {d.line} [{d.code}] {d.message}"))
    blocks = warns = 0
    resolved = []
    if directory is not None:
        for task in result.tasks:
            r = resolve_task(task, directory)
            resolved.append(r)
            for b in r.diagnostics:
                blocks += b.level == "error"
                warns += b.level == "warning"
                print(_show(f"{'BLOCK' if b.level == 'error' else 'WARNING'} [{b.code}] {b.message}"))
    if args.check_github:
        try:
            client = GitHubClient(transport=transport)
        except GitHubError as e:
            print(_show(f"LỖI: {e.message}"))
            return 2
        try:
            for b in validate_against_github(client, args.repo, result.tasks, resolved):
                blocks += 1
                print(_show(f"BLOCK [{b.code}] {b.message}"))
        finally:
            client.close()
    errors = len(result.errors)
    print(f"{len(result.tasks)} task, {errors} lỗi, {len(result.diagnostics) - errors + warns} cảnh báo, {blocks} BLOCK.")
    return 1 if errors or blocks else 0


def _plan(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    """Lập plan CHỈ ĐỌC (GET): ghi PREVIEW.md/PREVIEW.json/plan.json trong workspace, không ghi GitHub."""
    try:
        text, source = load_markdown(args.input, args.workspace)
        directory = load_members(args.members, args.workspace)
    except (LoaderError, MembersError) as e:
        print(_show(f"LỖI: {e}"))
        return 2
    result = parse_markdown(text, source)
    for d in result.diagnostics:
        print(_show(f"{d.level.upper()} dòng {d.line} [{d.code}] {d.message}"))
    if result.errors:  # file lỗi: không lập plan (tránh task mất im lặng khỏi plan được duyệt)
        print("LỖI: file Markdown có lỗi, không lập plan.")
        return 1
    resolved = [resolve_task(t, directory) for t in result.tasks]
    try:
        client = GitHubClient(transport=transport)
    except GitHubError as e:
        print(_show(f"LỖI: {e.message}"))
        return 2
    try:
        gh_diags = validate_against_github(client, args.repo, result.tasks, resolved)
        issues = None
        if not any(d.task_id is None for d in gh_diags):
            owner, _, name = args.repo.partition("/")
            try:
                issues = client.list_issues(owner, name)
            except GitHubError as e:
                gh_diags.append(Diagnostic(level="error", code=e.code, message=e.message))
        try:
            plan = build_plan(args.repo, source, result.tasks, resolved, gh_diags, issues, args.max_tasks)
            paths = write_preview(plan, args.output_dir, args.workspace)
        except PlanError as e:
            print(_show(f"LỖI: {e}"))
            return 1
    finally:
        client.close()
    blocked = sum(i.action == "BLOCK" for i in plan.items)
    for p in paths:
        print(_show(f"Đã ghi {p.relative_to(Path(args.workspace).resolve())}"))
    print(f"{len(plan.items)} task, {blocked} BLOCK. Plan SHA256: {plan.plan_sha256}")
    print("Write actions: DISABLED until approval")
    return 1 if blocked else 0


def _apply(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    """GHI GitHub (tạo issue) từ plan đã duyệt. Mặc định từ chối; cần --write và --approve-hash đúng."""
    try:
        code, lines = apply_plan(args.plan, args.approve_hash, args.write, args.repo, args.workspace, transport)
    except ApplyRejected as e:
        print(_show(f"TỪ CHỐI: {e}"))
        return 2
    for ln in lines:
        print(_show(ln))
    print(f"Hoàn tất: exit {code}.")
    return code


_DEPENDABOT_GROUPS = {"pip": "Runtime", "uv": "Runtime", "github_actions": "CI"}
_DEPENDABOT_REF = re.compile(r"dependabot/([A-Za-z0-9_-]+)/.+")
_MARKDOWN_TEXT = str.maketrans({
    ch: f"&#{ord(ch)};" for ch in punctuation.replace("-", "") + "\u2028\u2029"
})


def _dependabot_report(args: argparse.Namespace, transport: httpx.BaseTransport | None) -> int:
    custom: dict[str, str] = {}
    for option in args.group:
        ecosystem, _, name = option.partition("=")
        key = ecosystem.strip().lower().replace("-", "_")
        if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", key) or not name.strip() or key in custom:
            print("LỖI: --group cần ECOSYSTEM=NAME hợp lệ và mỗi ecosystem chỉ được khai báo một lần.")
            return 2
        custom[key] = name.strip()

    owner, _, repo = args.repo.partition("/")
    try:
        client = GitHubClient(transport=transport)
        try:
            pulls = client.list_dependabot_pulls(owner, repo)
        finally:
            client.close()
    except GitHubError as error:
        print(f"LỖI [{error.code}]: Không thể lập báo cáo Dependabot.")
        return 2

    groups = _DEPENDABOT_GROUPS | custom
    sections: dict[str, list[str]] = {}
    for pull in pulls:
        match = _DEPENDABOT_REF.fullmatch(pull.ref)
        ecosystem = match[1].lower().replace("-", "_") if match else ""
        name = groups.get(ecosystem, "Chưa phân nhóm")
        url = f"https://github.com/{owner}/{repo}/pull/{pull.number}"
        title = _show(pull.title).translate(_MARKDOWN_TEXT)
        sections.setdefault(name, []).append(f"- [#{pull.number}]({url}) {title}")

    print("# Pull request Dependabot đang mở")
    if not sections:
        print("\nKhông có pull request Dependabot đang mở.")
    for name, rows in sections.items():
        print(f"\n## {_show(name).translate(_MARKDOWN_TEXT)}\n")
        print("\n".join(rows))
    return 0


def _dependency_pr_plan(args: argparse.Namespace) -> int:
    try:
        plan = dependency_pr_plan(args.workspace, args.repo, args.branch, args.title, args.file)
    except (GitHubError, PlanError) as error:
        print(_show(f"LỖI: {error}"))
        return 2
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def _dependency_pr_apply(args: argparse.Namespace) -> int:
    try:
        result = dependency_pr_apply(args.workspace, args.plan, args.approve_hash, args.write)
    except (GitHubError, PlanError) as error:
        print(_show(f"LỖI: {error}"))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="github-bot",
        description="Bot chuyển Markdown công việc thành GitHub Issues. Hiện chưa có lệnh ghi GitHub.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="group")
    issues = sub.add_parser("issues", help="Thao tác với danh sách task").add_subparsers(
        dest="cmd", required=True
    )
    v = issues.add_parser("validate", help="Kiểm tra file Markdown (offline)")
    v.add_argument("--input", required=True, help="File .md nằm trong workspace")
    v.add_argument("--members", help="Danh bạ members.yaml (trong workspace). KHÔNG truyền thì validate không phân giải người phụ trách")
    v.add_argument("--workspace", type=Path, default=Path.cwd(), help="Thư mục gốc cho phép đọc")
    v.add_argument("--check-github", action="store_true", help="OPT-IN: gọi GitHub (chỉ GET) kiểm tra repo, assignee, label; cần GITHUB_TOKEN")
    v.add_argument("--repo", help="OWNER/REPO cho --check-github (không có mặc định)")
    v.set_defaults(func=lambda a: _validate(a, transport))
    pl = issues.add_parser("plan", help="Lập plan CHỈ ĐỌC (cần GITHUB_TOKEN); không ghi GitHub")
    pl.add_argument("--input", required=True, help="File .md nằm trong workspace")
    pl.add_argument("--members", required=True, help="Danh bạ members.yaml (trong workspace)")
    pl.add_argument("--repo", required=True, help="OWNER/REPO (không có mặc định)")
    pl.add_argument("--output-dir", required=True, help="Thư mục trong workspace để ghi PREVIEW.md/PREVIEW.json/plan.json")
    pl.add_argument("--max-tasks", type=int, default=50, help="max_tasks_per_run (mặc định 50)")
    pl.add_argument("--workspace", type=Path, default=Path.cwd(), help="Thư mục gốc cho phép đọc/ghi")
    pl.set_defaults(func=lambda a: _plan(a, transport))
    ap = issues.add_parser("apply", help="GHI: tạo issue từ plan đã duyệt (cần --write và --approve-hash)")
    ap.add_argument("--plan", required=True, help="plan.json trong workspace")
    ap.add_argument("--approve-hash", required=True, help="SHA-256 của plan (từ `issues plan`); không có approve-all")
    ap.add_argument("--write", action="store_true", help="Bật ghi GitHub tường minh (mặc định từ chối)")
    ap.add_argument("--repo", help="Nếu có, phải khớp repo trong plan")
    ap.add_argument("--workspace", type=Path, default=Path.cwd(), help="Thư mục gốc cho phép đọc/ghi")
    ap.set_defaults(func=lambda a: _apply(a, transport))
    dependabot = sub.add_parser("dependabot", help="Báo cáo pull request Dependabot")
    report = dependabot.add_subparsers(dest="cmd", required=True).add_parser(
        "report", help="Báo cáo Markdown chỉ đọc từ GitHub"
    )
    report.add_argument("--repo", required=True, help="OWNER/REPO")
    report.add_argument(
        "--group", action="append", default=[], metavar="ECOSYSTEM=NAME",
        help="Gán ecosystem vào nhóm, có thể dùng nhiều lần",
    )
    report.set_defaults(func=lambda a: _dependabot_report(a, transport))
    dependencies = sub.add_parser("dependencies", help="Preview cập nhật phụ thuộc")
    dependency_commands = dependencies.add_subparsers(dest="cmd", required=True)
    pr_plan = dependency_commands.add_parser(
        "pr-plan", help="In kế hoạch PR từ Git index cục bộ",
    )
    pr_plan.add_argument("--workspace", type=Path, default=Path.cwd(), help="Gốc Git worktree")
    pr_plan.add_argument("--repo", required=True, help="Repo đích OWNER/REPO")
    pr_plan.add_argument("--branch", required=True, help="Tên nhánh dự kiến")
    pr_plan.add_argument("--title", required=True, help="Tiêu đề PR dự kiến")
    pr_plan.add_argument(
        "--file", action="append", required=True, help="Tệp staged pyproject.toml hoặc uv.lock",
    )
    pr_plan.set_defaults(func=_dependency_pr_plan)
    pr_apply = dependency_commands.add_parser("pr-apply", help="Tạo nhánh và commit Git cục bộ")
    pr_apply.add_argument("--workspace", type=Path, required=True, help="Gốc Git worktree")
    pr_apply.add_argument("--plan", required=True, help="Tệp plan JSON trong workspace")
    pr_apply.add_argument("--approve-hash", required=True, help="SHA-256 của plan đã duyệt")
    pr_apply.add_argument("--write", action="store_true", help="Cho phép ghi nhánh Git cục bộ")
    pr_apply.set_defaults(func=_dependency_pr_apply)
    args, extra = parser.parse_known_args(argv)
    if extra:  # không in lại tham số (có thể là bí mật gõ nhầm như --token)
        print(f"LỖI: {len(extra)} tham số không được hỗ trợ (xem --help).")
        return 2
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))
