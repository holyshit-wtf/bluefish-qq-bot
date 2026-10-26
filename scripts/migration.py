from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import tomllib
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_settings
from storage.schema import SCHEMA_VERSION


MANIFEST_FORMAT_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024
DEPLOYMENT_DOCUMENT = Path("docs") / "guides" / "跨电脑迁移与轮换部署.md"
SKIPPED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".testdeps",
    "__pycache__",
    "dist",
    "logs",
}


class MigrationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        staging.write_text(content, encoding="utf-8", newline="\n")
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def _write_checksum_file(path: Path) -> Path:
    checksum_path = path.with_name(f"{path.name}.sha256")
    _atomic_write_text(
        checksum_path,
        f"{sha256_file(path)}  {path.name}\n",
    )
    return checksum_path


def _sqlite_uri(path: Path, mode: str) -> str:
    return f"{path.resolve().as_uri()}?mode={mode}"


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def assert_bot_stopped(lock_path: Path) -> None:
    if not lock_path.is_file():
        return
    try:
        pid = int(lock_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return
    if process_is_running(pid):
        raise MigrationError(
            f"机器人仍在运行（PID {pid}）。请先停止机器人和 QQ/NapCat。"
        )


def inspect_database(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise MigrationError(f"数据库不存在：{path}")
    try:
        with closing(
            sqlite3.connect(_sqlite_uri(path, "ro"), uri=True)
        ) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise MigrationError("SQLite integrity_check 未通过")
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {"groups", "messages_fts", "system_settings"}
            missing = sorted(required - tables)
            if missing:
                raise MigrationError(
                    "数据库缺少必要数据表：" + "、".join(missing)
                )
            try:
                connection.execute(
                    "SELECT rowid FROM messages_fts LIMIT 1"
                ).fetchone()
            except sqlite3.Error as exc:
                raise MigrationError(
                    f"SQLite FTS5 检查失败：{exc}"
                ) from exc
    except sqlite3.Error as exc:
        raise MigrationError(
            f"数据库无法读取：{type(exc).__name__}: {exc}"
        ) from exc
    if schema_version < 1:
        raise MigrationError("数据库 Schema 版本无效")
    if schema_version > SCHEMA_VERSION:
        raise MigrationError(
            f"数据库 Schema v{schema_version} 高于当前程序支持的 "
            f"v{SCHEMA_VERSION}，请先更新程序"
        )
    return {
        "schema_version": schema_version,
        "integrity": "ok",
        "fts5": True,
        "size": path.stat().st_size,
    }


def _backup_database(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        with closing(
            sqlite3.connect(_sqlite_uri(source, "ro"), uri=True)
        ) as source_connection:
            with closing(sqlite3.connect(destination)) as target_connection:
                source_connection.backup(target_connection)
                target_connection.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise MigrationError(
            f"SQLite 一致性备份失败：{type(exc).__name__}: {exc}"
        ) from exc


def _application_version(project_root: Path) -> str:
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        return "unknown"
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return "unknown"
    version = data.get("project", {}).get("version")
    return str(version) if version else "unknown"


def _package_manifest(
    *,
    project_root: Path,
    package_type: str,
    database_relative_path: str,
    database_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "format_version": MANIFEST_FORMAT_VERSION,
        "package_type": package_type,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app_version": _application_version(project_root),
        "schema_version": metadata["schema_version"],
        "database_path": database_relative_path,
        "database_size": metadata["size"],
        "database_sha256": sha256_file(database_path),
        "contains_secret": package_type == "deployment",
        "python_requires": "3.12",
    }


def _database_checksum_text(manifest: dict[str, Any]) -> str:
    return (
        f"{manifest['database_sha256']}  "
        f"{manifest['database_path']}\n"
    )


def _write_zip_entry(
    archive: zipfile.ZipFile,
    source: Path,
    archive_name: str,
) -> None:
    info = zipfile.ZipInfo.from_file(source, archive_name)
    info.compress_type = zipfile.ZIP_DEFLATED
    with source.open("rb") as source_handle:
        with archive.open(
            info,
            "w",
            force_zip64=source.stat().st_size >= 2**31,
        ) as target_handle:
            shutil.copyfileobj(
                source_handle,
                target_handle,
                length=1024 * 1024,
            )


def _write_zip_bytes(
    archive: zipfile.ZipFile,
    content: bytes,
    archive_name: str,
) -> None:
    info = zipfile.ZipInfo(archive_name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, content)


def _include_deployment_file(path: Path, project_root: Path) -> bool:
    relative = path.relative_to(project_root)
    if any(part in SKIPPED_DIRECTORIES for part in relative.parts):
        return False
    if relative.parts and relative.parts[0] == "data":
        return False
    if path.suffix in {".pyc", ".log"}:
        return False
    if path.name.endswith(
        (".db", ".db-wal", ".db-shm", ".lock", ".tmp")
    ):
        return False
    return path.is_file()


def create_snapshot_archive(
    *,
    project_root: Path,
    database_path: Path,
    output_directory: Path,
) -> tuple[Path, Path]:
    project_root = project_root.resolve()
    database_path = database_path.resolve()
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    inspect_database(database_path)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    archive_path = output_directory / f"bluefish-snapshot-{timestamp}.zip"
    if archive_path.exists():
        raise MigrationError(f"目标快照已存在：{archive_path}")

    with tempfile.TemporaryDirectory(prefix="bluefish-snapshot-") as temp:
        temp_root = Path(temp)
        snapshot_db = temp_root / "snapshot" / "bluefish.db"
        _backup_database(database_path, snapshot_db)
        metadata = inspect_database(snapshot_db)
        manifest = _package_manifest(
            project_root=project_root,
            package_type="snapshot",
            database_relative_path="snapshot/bluefish.db",
            database_path=snapshot_db,
            metadata=metadata,
        )
        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            _write_zip_entry(archive, snapshot_db, "snapshot/bluefish.db")
            _write_zip_bytes(
                archive,
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
                "manifest.json",
            )
            _write_zip_bytes(
                archive,
                _database_checksum_text(manifest).encode("utf-8"),
                "snapshot.sha256",
            )
    checksum_path = _write_checksum_file(archive_path)
    return archive_path, checksum_path


def create_deployment_archive(
    *,
    project_root: Path,
    database_path: Path,
    output_directory: Path,
) -> tuple[Path, Path]:
    project_root = project_root.resolve()
    database_path = database_path.resolve()
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    env_path = project_root / ".env"
    if not env_path.is_file():
        raise MigrationError("完整部署包必须包含项目根目录的 .env")
    deployment_document = project_root / DEPLOYMENT_DOCUMENT
    if not deployment_document.is_file():
        raise MigrationError(f"缺少部署文档：{deployment_document}")
    inspect_database(database_path)

    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    archive_path = output_directory / f"bluefish-deployment-{timestamp}.zip"
    if archive_path.exists():
        raise MigrationError(f"目标部署包已存在：{archive_path}")

    with tempfile.TemporaryDirectory(prefix="bluefish-deployment-") as temp:
        temp_root = Path(temp)
        snapshot_db = temp_root / "snapshot" / "bluefish.db"
        _backup_database(database_path, snapshot_db)
        metadata = inspect_database(snapshot_db)
        database_relative = "蓝色大肥鱼/data/bluefish.db"
        manifest = _package_manifest(
            project_root=project_root,
            package_type="deployment",
            database_relative_path=database_relative,
            database_path=snapshot_db,
            metadata=metadata,
        )

        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in sorted(project_root.rglob("*")):
                if not _include_deployment_file(path, project_root):
                    continue
                relative = path.relative_to(project_root)
                _write_zip_entry(
                    archive,
                    path,
                    f"蓝色大肥鱼/{relative.as_posix()}",
                )
            _write_zip_entry(archive, snapshot_db, database_relative)
            _write_zip_bytes(
                archive,
                deployment_document.read_bytes(),
                "公司电脑部署说明.md",
            )
            _write_zip_bytes(
                archive,
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
                "manifest.json",
            )
            _write_zip_bytes(
                archive,
                _database_checksum_text(manifest).encode("utf-8"),
                "package.sha256",
            )
    checksum_path = _write_checksum_file(archive_path)
    return archive_path, checksum_path


def _safe_archive_name(name: str) -> PurePosixPath:
    if "\\" in name:
        raise MigrationError(f"ZIP 包含非法路径：{name}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MigrationError(f"ZIP 包含越界路径：{name}")
    return path


def _zip_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        safe_name = str(_safe_archive_name(info.filename))
        if safe_name in members:
            raise MigrationError(f"ZIP 包含重复条目：{safe_name}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise MigrationError(f"ZIP 不允许包含符号链接：{safe_name}")
        members[safe_name] = info
    return members


def _read_manifest(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> dict[str, Any]:
    info = members.get("manifest.json")
    if info is None:
        raise MigrationError("ZIP 缺少 manifest.json")
    if info.file_size > MAX_MANIFEST_BYTES:
        raise MigrationError("manifest.json 过大")
    try:
        manifest = json.loads(archive.read(info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError("manifest.json 不是有效的 UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise MigrationError("manifest.json 顶层必须是对象")
    if manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise MigrationError("不支持的迁移包格式版本")
    if manifest.get("package_type") not in {"deployment", "snapshot"}:
        raise MigrationError("manifest.json 的 package_type 无效")
    required = {
        "package_type",
        "created_at",
        "app_version",
        "schema_version",
        "database_path",
        "database_sha256",
        "contains_secret",
        "python_requires",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise MigrationError(
            "manifest.json 缺少字段：" + "、".join(missing)
        )
    database_relative = manifest.get("database_path")
    if not isinstance(database_relative, str):
        raise MigrationError("manifest.json 的 database_path 无效")
    _safe_archive_name(database_relative)
    expected_hash = manifest.get("database_sha256")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
    ):
        raise MigrationError("manifest.json 的数据库 SHA-256 无效")
    if manifest.get("python_requires") != "3.12":
        raise MigrationError("迁移包要求的 Python 版本不是 3.12")
    return manifest


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source:
        with destination.open("wb") as target:
            shutil.copyfileobj(source, target)


def _extract_verified_archive(
    archive_path: Path,
    *,
    expected_type: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path, tempfile.TemporaryDirectory[str]]:
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise MigrationError(f"迁移包不存在：{archive_path}")
    try:
        archive_context = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise MigrationError(
            f"迁移包不是有效的 ZIP：{type(exc).__name__}: {exc}"
        ) from exc
    temp_context = tempfile.TemporaryDirectory(prefix="bluefish-verify-")
    temp_root = Path(temp_context.name)
    try:
        with archive_context as archive:
            members = _zip_members(archive)
            manifest = _read_manifest(archive, members)
            if (
                expected_type is not None
                and manifest["package_type"] != expected_type
            ):
                raise MigrationError(
                    f"迁移包类型应为 {expected_type}，"
                    f"实际为 {manifest['package_type']}"
                )
            database_relative = str(manifest["database_path"])
            info = members.get(database_relative)
            if info is None:
                raise MigrationError("ZIP 缺少 manifest 指定的数据库")
            if info.file_size > MAX_DATABASE_BYTES:
                raise MigrationError("迁移数据库超过 2 GiB 安全上限")
            extracted = temp_root / "verified.db"
            _extract_member(archive, info, extracted)
        actual_hash = sha256_file(extracted)
        if actual_hash != manifest["database_sha256"]:
            raise MigrationError("数据库 SHA-256 校验失败")
        metadata = inspect_database(extracted)
        if metadata["schema_version"] != manifest["schema_version"]:
            raise MigrationError("manifest 与数据库 Schema 版本不一致")
        return manifest, metadata, extracted, temp_context
    except BaseException:
        temp_context.cleanup()
        raise


def verify_archive(
    archive_path: Path,
    *,
    expected_type: str | None = None,
) -> dict[str, Any]:
    manifest, metadata, _, temp_context = _extract_verified_archive(
        archive_path,
        expected_type=expected_type,
    )
    temp_context.cleanup()
    return {
        "archive": str(archive_path.resolve()),
        "manifest": manifest,
        "database": metadata,
    }


def import_snapshot_archive(
    *,
    archive_path: Path,
    database_path: Path,
    backup_directory: Path,
    lock_path: Path | None = None,
    clear_lock: bool = False,
) -> dict[str, Any]:
    database_path = database_path.resolve()
    backup_directory = backup_directory.resolve()
    manifest, metadata, verified_database, temp_context = (
        _extract_verified_archive(archive_path, expected_type="snapshot")
    )
    staging = database_path.with_name(
        f".{database_path.name}.import-{os.getpid()}.tmp"
    )
    backup_path: Path | None = None
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        if database_path.is_file():
            inspect_database(database_path)
            backup_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().astimezone().strftime(
                "%Y%m%d-%H%M%S-%f"
            )
            backup_path = backup_directory / f"pre-import-{stamp}.db"
            _backup_database(database_path, backup_path)
            inspect_database(backup_path)

        shutil.copy2(verified_database, staging)
        inspect_database(staging)
        os.replace(staging, database_path)
        for suffix in ("-wal", "-shm"):
            database_path.with_name(database_path.name + suffix).unlink(
                missing_ok=True
            )
        if clear_lock and lock_path is not None:
            lock_path.unlink(missing_ok=True)
        inspect_database(database_path)
    finally:
        staging.unlink(missing_ok=True)
        temp_context.cleanup()
    return {
        "database": str(database_path),
        "backup": str(backup_path) if backup_path else None,
        "manifest": manifest,
        "database_info": metadata,
    }


def _project_settings(project_root: Path):
    from dotenv import load_dotenv

    project_root = project_root.resolve()
    load_dotenv(project_root / ".env", override=False)
    return load_settings(project_root)


def _print_paths(archive_path: Path, checksum_path: Path) -> None:
    print(f"迁移包：{archive_path}")
    print(f"ZIP 校验文件：{checksum_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="蓝色大肥鱼迁移工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="导出一致性数据快照")
    snapshot.add_argument("--project-root", type=Path, required=True)
    snapshot.add_argument("--output-directory", type=Path, required=True)

    deployment = subparsers.add_parser("deployment", help="生成完整部署包")
    deployment.add_argument("--project-root", type=Path, required=True)
    deployment.add_argument("--output-directory", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="校验迁移包")
    verify.add_argument("--archive", type=Path, required=True)

    restore = subparsers.add_parser("import", help="导入数据快照")
    restore.add_argument("--project-root", type=Path, required=True)
    restore.add_argument("--archive", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command in {"snapshot", "deployment"}:
            project_root = arguments.project_root.resolve()
            settings = _project_settings(project_root)
            assert_bot_stopped(settings.data_dir / "bot.lock")
            if arguments.command == "snapshot":
                archive_path, checksum_path = create_snapshot_archive(
                    project_root=project_root,
                    database_path=settings.database_path,
                    output_directory=arguments.output_directory,
                )
            else:
                archive_path, checksum_path = create_deployment_archive(
                    project_root=project_root,
                    database_path=settings.database_path,
                    output_directory=arguments.output_directory,
                )
            _print_paths(archive_path, checksum_path)
            return 0
        if arguments.command == "verify":
            result = verify_archive(arguments.archive)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if arguments.command == "import":
            project_root = arguments.project_root.resolve()
            settings = _project_settings(project_root)
            assert_bot_stopped(settings.data_dir / "bot.lock")
            result = import_snapshot_archive(
                archive_path=arguments.archive,
                database_path=settings.database_path,
                backup_directory=settings.backup_dir,
                lock_path=settings.data_dir / "bot.lock",
                clear_lock=True,
            )
            print(f"数据库已导入：{result['database']}")
            if result["backup"]:
                print(f"导入前备份：{result['backup']}")
            print(
                "Schema："
                f"v{result['database_info']['schema_version']}；"
                "integrity_check=ok；FTS5=可用"
            )
            return 0
    except (
        MigrationError,
        OSError,
        sqlite3.Error,
        zipfile.BadZipFile,
    ) as exc:
        print(f"[迁移失败] {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
