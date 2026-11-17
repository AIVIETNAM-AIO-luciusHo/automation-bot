import argparse
from pathlib import Path

from github_bot import __version__
from github_bot.markdown.loader import LoaderError, load_markdown
from github_bot.markdown.parsers import parse_markdown


def _validate(args: argparse.Namespace) -> int:
    """Offline: chỉ đọc file, không gọi GitHub, không ghi gì."""
    try:
        text, source = load_markdown(args.input, args.workspace)
    except LoaderError as e:
        print(f"LỖI: {e}")
        return 2
    result = parse_markdown(text, source)
    for d in result.diagnostics:
        print(f"{d.level.upper()} dòng {d.line} [{d.code}] {d.message}")
    errors = len(result.errors)
    print(f"{len(result.tasks)} task, {errors} lỗi, {len(result.diagnostics) - errors} cảnh báo.")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
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
    v.add_argument("--members", help="Nhận cờ ở M1; ánh xạ tên -> login thuộc M2, chưa đọc file")
    v.add_argument("--workspace", type=Path, default=Path.cwd(), help="Thư mục gốc cho phép đọc")
    v.set_defaults(func=_validate)
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))
