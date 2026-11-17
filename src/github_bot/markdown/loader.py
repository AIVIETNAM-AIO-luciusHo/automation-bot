from pathlib import Path

MAX_BYTES = 1_000_000


class LoaderError(Exception):
    pass


def load_markdown(path: str | Path, workspace: str | Path) -> tuple[str, str]:
    """Đọc file .md nằm trong workspace. Trả về (nội dung, source_file chuẩn hóa dạng posix)."""
    return read_workspace_file(path, workspace, (".md",))


def read_workspace_file(
    path: str | Path, workspace: str | Path, suffixes: tuple[str, ...]
) -> tuple[str, str]:
    root = Path(workspace).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    target = target.resolve()  # resolve symlink trước khi kiểm tra vùng cho phép
    if not target.is_relative_to(root):
        raise LoaderError("Đường dẫn nằm ngoài workspace, bị chặn.")
    if target.suffix.lower() not in suffixes or not target.is_file():
        raise LoaderError(f"Chỉ đọc file {'/'.join(suffixes)} tồn tại.")
    if target.stat().st_size > MAX_BYTES:
        raise LoaderError(f"File lớn hơn {MAX_BYTES} byte.")
    try:
        text = target.read_text(encoding="utf-8-sig")  # bỏ BOM
    except UnicodeDecodeError as e:
        raise LoaderError("File không phải UTF-8.") from e
    return text, target.relative_to(root).as_posix()
