import argparse

from github_bot import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="github-bot",
        description="Bot chuyển Markdown công việc thành GitHub Issues. M0: chưa có lệnh ghi GitHub.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    return 0
