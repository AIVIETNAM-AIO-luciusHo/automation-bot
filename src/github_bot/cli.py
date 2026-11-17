import argparse
from pathlib import Path

import httpx

from github_bot import __version__
from github_bot.github.client import GitHubClient, GitHubError
from github_bot.github.validator import validate_against_github
from github_bot.identities.resolver import MembersError, load_members, resolve_task
from github_bot.markdown.loader import LoaderError, load_markdown
from github_bot.markdown.parsers import parse_markdown


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
        print(f"LỖI: {e}")
        return 2
    directory = None
    if args.members is not None:
        try:
            directory = load_members(args.members, args.workspace)
        except MembersError as e:
            print(f"LỖI danh bạ: {e}")
            return 2
    result = parse_markdown(text, source)
    for d in result.diagnostics:
        print(f"{d.level.upper()} dòng {d.line} [{d.code}] {d.message}")
    blocks = warns = 0
    resolved = []
    if directory is not None:
        for task in result.tasks:
            r = resolve_task(task, directory)
            resolved.append(r)
            for b in r.diagnostics:
                blocks += b.level == "error"
                warns += b.level == "warning"
                print(f"{'BLOCK' if b.level == 'error' else 'WARNING'} [{b.code}] {b.message}")
    if args.check_github:
        try:
            client = GitHubClient(transport=transport)
        except GitHubError as e:
            print(f"LỖI: {e.message}")
            return 2
        try:
            for b in validate_against_github(client, args.repo, result.tasks, resolved):
                blocks += 1
                print(f"BLOCK [{b.code}] {b.message}")
        finally:
            client.close()
    errors = len(result.errors)
    print(f"{len(result.tasks)} task, {errors} lỗi, {len(result.diagnostics) - errors + warns} cảnh báo, {blocks} BLOCK.")
    return 1 if errors or blocks else 0


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
    args, extra = parser.parse_known_args(argv)
    if extra:  # không in lại tham số (có thể là bí mật gõ nhầm như --token)
        print(f"LỖI: {len(extra)} tham số không được hỗ trợ (xem --help).")
        return 2
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))
