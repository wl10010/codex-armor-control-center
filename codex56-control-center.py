#!/usr/bin/env python3
"""Tkinter GUI for the Codex 5.6 deployment tool."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import runpy
import shlex
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import List, Optional

try:
    import tomllib
except ImportError:  # Python 3.10 fallback for the simple generated entry.
    tomllib = None


MIN_PYTHON = (3, 10)
APP_TITLE = "coedx 5.6 破甲 可视化部署"
CORE_SCRIPT_NAME = "codex56-orchestrator.py"
CORE_CLI_FLAG = "--keysmith-core-cli"
REPAIR_CLI_FLAG = "--repair-config-conflict"
MANIFEST_FILENAME = ".codex-keysmith-manifest.json"
JOURNAL_PREFIX = ".codex-keysmith-transaction-"
CLEANUP_MARKER_PREFIX = ".codex-keysmith-cleanup-"
CLEANUP_MARKER_SUFFIX = ".intent.json"
FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
CORE_SCRIPT = BASE_DIR / CORE_SCRIPT_NAME
DISPLAY_HIDDEN_TERMS = ("成人", "武器")
TRANSACTION_RESIDUE_PATTERNS = (
    ".keysmith-hooks-*",
    ".keysmith-restore-*",
    ".keysmith-write-*",
    ".keysmith-uninstall-*",
    f"{JOURNAL_PREFIX}*",
    f"{CLEANUP_MARKER_PREFIX}*{CLEANUP_MARKER_SUFFIX}",
)
TOML_STRING_RE = re.compile(
    r'^"(?:\\.|[^"\\])*"$|^\'(?:\\.|[^\'\\])*\'$'
)

STATUS_STYLES = {
    "checking": ("检查中...", "#E8F1FB", "#005A9E"),
    "recovery": ("事务待恢复", "#FFF4CE", "#7A5D00"),
    "conflict": ("已部署（配置冲突）", "#FDE7E9", "#A4262C"),
    "degraded": ("已部署（维护受阻）", "#FFF4CE", "#7A5D00"),
    "blocked": ("部署受阻", "#FDE7E9", "#A4262C"),
    "deployed": ("已部署", "#DFF6DD", "#0B6A0B"),
    "failed": ("检查失败", "#FDE7E9", "#A4262C"),
    "not_deployed": ("未部署", "#EDEBE9", "#605E5C"),
}


OPERATIONS = {
    "部署预览（不修改文件）": "deploy_preview",
    "执行部署": "deploy",
    "查看当前状态": "status",
    "卸载预览（不修改文件）": "uninstall_preview",
    "执行卸载": "uninstall",
    "恢复事务预览（不修改文件）": "recover_preview",
    "执行事务恢复": "recover",
    "恢复 hooks.json": "restore_hooks",
    "显示脚本版本": "version",
    "显示命令帮助": "help",
}

DEPLOY_OPERATIONS = {"deploy_preview", "deploy"}
CODEX_DIR_OPERATIONS = {
    "deploy_preview",
    "deploy",
    "status",
    "uninstall_preview",
    "uninstall",
    "recover_preview",
    "recover",
    "restore_hooks",
}


def require_supported_python() -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(map(str, MIN_PYTHON))
        current = ".".join(map(str, sys.version_info[:3]))
        raise RuntimeError(f"需要 Python {required} 或更高版本，当前版本为 {current}")


def core_command_prefix() -> List[str]:
    if FROZEN:
        return [sys.executable, CORE_CLI_FLAG]
    return [sys.executable, str(CORE_SCRIPT)]


def format_command(command: List[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def run_bundled_core() -> None:
    if not CORE_SCRIPT.is_file():
        raise SystemExit("Bundled core resources are missing.")
    sys.argv = [str(CORE_SCRIPT), *sys.argv[2:]]
    runpy.run_path(str(CORE_SCRIPT), run_name="__main__")


def _run_core_command(args: List[str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*core_command_prefix(), *args],
        cwd=str(BASE_DIR),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=False,
    )


@dataclass
class ManagedStatus:
    """GUI status based only on content managed by this tool."""

    status: str
    codex_dir: str = ""
    has_manifest: bool = False
    has_config: bool = False
    md_path: str = ""
    model_instructions_file: Optional[str] = None
    expected_md_reference: str = ""
    residue: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    rollback_issues: List[str] = field(default_factory=list)
    details: List[str] = field(default_factory=list)

    @property
    def is_managed_conflict(self) -> bool:
        return self.status == "conflict"

    def summary_text(self) -> str:
        lines = [
            f"[Managed status] directory: {self.codex_dir or '<unset>'}",
            f"[Managed status] result: {self.status}",
            f"[Managed status] deployment manifest: "
            f"{'present' if self.has_manifest else 'absent'}",
            f"[Managed status] config.toml: "
            f"{'present' if self.has_config else 'absent'}",
            f"[Managed status] model_instructions_file: "
            f"{self.model_instructions_file if self.model_instructions_file is not None else '<unset or unreadable>'}",
            f"[Managed status] expected instruction: "
            f"{self.expected_md_reference or '<unknown>'}",
            f"[Managed status] managed markdown: {self.md_path or '<unknown>'}",
            f"[Managed status] transaction residue: "
            f"{', '.join(self.residue) if self.residue else 'none'}",
        ]
        if self.issues:
            lines.append("[Managed status] issues:")
            lines.extend(f"  - {issue}" for issue in self.issues)
        if self.rollback_issues:
            lines.append("[Managed status] rollback issues:")
            lines.extend(f"  - {issue}" for issue in self.rollback_issues)
        if self.details:
            lines.append("[Managed status] notes:")
            lines.extend(f"  - {detail}" for detail in self.details)
        return "\n".join(lines) + "\n"


def resolve_codex_dir(codex_dir: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(codex_dir))).resolve()


def _path_entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _list_transaction_residue(codex_dir: Path) -> List[str]:
    residue: List[Path] = []
    for pattern in TRANSACTION_RESIDUE_PATTERNS:
        try:
            residue.extend(
                path for path in codex_dir.glob(pattern) if _path_entry_exists(path)
            )
        except OSError:
            continue
    return sorted({str(path) for path in residue})


def _decode_toml_string(raw: str) -> Optional[str]:
    value = raw.strip()
    if not TOML_STRING_RE.fullmatch(value):
        return None
    quote = value[0]
    body = value[1:-1]
    if quote == "'":
        return body
    try:
        return bytes(body, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return None


def read_model_instructions_file(config_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Return (reference, error). reference may be None when unset."""
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"无法读取 config.toml: {exc}"
    except UnicodeDecodeError as exc:
        return None, f"config.toml 不是有效 UTF-8: {exc}"

    if tomllib is not None:
        try:
            parsed = tomllib.loads(content)
        except Exception as exc:
            return None, f"config.toml 不是有效 TOML: {exc}"
        reference = parsed.get("model_instructions_file")
        if reference is None:
            return None, None
        if not isinstance(reference, str):
            return None, "顶层 model_instructions_file 不是字符串"
        return reference, None

    references: List[str] = []
    in_table = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_table = True
            continue
        if in_table or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "model_instructions_file":
            continue
        decoded = _decode_toml_string(value)
        if decoded is None:
            return None, "顶层 model_instructions_file 不是可识别的字符串"
        references.append(decoded)

    if len(references) > 1:
        return None, "发现重复的顶层 model_instructions_file"
    if not references:
        return None, None
    return references[0], None


def instruction_reference_matches(actual: Optional[str], md_filename: str) -> bool:
    if actual is None:
        return False
    normalized = actual.replace("\\", "/") if os.name == "nt" else actual
    return normalized in {md_filename, f"./{md_filename}"}


def _load_manifest(manifest_path: Path) -> dict:
    with manifest_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("部署清单根节点必须是对象")
    return data


def inspect_managed_status(codex_dir: str) -> ManagedStatus:
    """
    Check only content managed by this tool:

    1. model_instructions_file points at the deployed markdown
    2. deployed markdown exists and content matches the manifest
    3. no transaction residue remains

    Other config.toml changes (models/MCP/plugins/Grok fields) are ignored.
    """
    if not codex_dir or not codex_dir.strip():
        return ManagedStatus(
            status="failed",
            issues=["未选择 Codex 配置目录"],
        )

    try:
        target = resolve_codex_dir(codex_dir)
    except OSError as exc:
        return ManagedStatus(
            status="failed",
            codex_dir=codex_dir,
            issues=[f"无法解析 Codex 目录: {exc}"],
        )

    result = ManagedStatus(status="not_deployed", codex_dir=str(target))
    if not target.is_dir():
        result.status = "failed"
        result.issues.append(f"Codex 目录不存在: {target}")
        return result

    config_path = target / "config.toml"
    manifest_path = target / MANIFEST_FILENAME
    result.has_config = _is_regular_file(config_path)
    result.has_manifest = _is_regular_file(manifest_path)

    try:
        residue = _list_transaction_residue(target)
    except OSError as exc:
        result.status = "failed"
        result.issues.append(f"无法检查事务残留: {exc}")
        return result
    result.residue = residue
    if residue:
        result.status = "recovery"
        result.issues.append("发现未完成事务目录")
        return result

    if not result.has_manifest:
        if result.has_config:
            result.status = "not_deployed"
            result.details.append("未找到部署清单")
        else:
            result.status = "not_deployed"
            result.details.append("未找到 config.toml 与部署清单")
        return result

    try:
        manifest = _load_manifest(manifest_path)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result.status = "conflict"
        result.issues.append(f"部署清单无效: {exc}")
        return result

    md_info = manifest.get("md")
    if not isinstance(md_info, dict):
        result.status = "conflict"
        result.issues.append("部署清单缺少 md 段")
        return result

    md_name = md_info.get("path")
    md_after = md_info.get("after")
    if not isinstance(md_name, str) or not md_name or "/" in md_name or "\\" in md_name:
        result.status = "conflict"
        result.issues.append("部署清单 md.path 无效")
        return result
    if not isinstance(md_after, dict):
        result.status = "conflict"
        result.issues.append("部署清单 md.after 无效")
        return result

    expected_size = md_after.get("size")
    expected_sha256 = md_after.get("sha256")
    if not isinstance(expected_size, int) or expected_size < 0:
        result.status = "conflict"
        result.issues.append("部署清单 md.after.size 无效")
        return result
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        result.status = "conflict"
        result.issues.append("部署清单 md.after.sha256 无效")
        return result

    result.expected_md_reference = f"./{md_name}"
    md_path = target / md_name
    result.md_path = str(md_path)

    if not result.has_config:
        result.status = "conflict"
        result.issues.append("config.toml 不存在")
        return result

    reference, reference_error = read_model_instructions_file(config_path)
    if reference_error:
        result.status = "conflict"
        result.issues.append(reference_error)
        return result
    result.model_instructions_file = reference
    if not instruction_reference_matches(reference, md_name):
        result.status = "conflict"
        actual = reference if reference is not None else "<未设置>"
        result.issues.append(
            f"model_instructions_file 指向不正确: 当前={actual}，期望=./{md_name}"
        )

    if not _is_regular_file(md_path):
        result.status = "conflict"
        result.issues.append(f"部署的 Markdown 不存在或不是普通文件: {md_path}")
    else:
        try:
            actual_size = md_path.stat().st_size
            actual_sha256 = _file_sha256(md_path)
        except OSError as exc:
            result.status = "conflict"
            result.issues.append(f"无法读取部署的 Markdown: {exc}")
        else:
            if actual_size != expected_size or actual_sha256 != expected_sha256:
                result.status = "conflict"
                result.issues.append(
                    "部署的 Markdown 内容已变化"
                    f"（当前 size={actual_size}, 期望 size={expected_size}, "
                    f"sha256={actual_sha256[:12]}...）"
                )
            else:
                result.details.append("部署的 Markdown 内容与清单一致")

    if result.issues:
        result.status = "conflict"
        result.details.append(
            "已忽略 config.toml 中模型/MCP/插件/Grok 等非托管字段变化"
        )
        return result

    result.status = "deployed"
    result.details.append("model_instructions_file 指向正确")
    result.details.append("无事务残留")
    result.details.append(
        "已忽略 config.toml 中模型/MCP/插件/Grok 等非托管字段变化"
    )
    return result


def _targeted_core_args(codex_dir: str, *args: str) -> List[str]:
    command = list(args)
    if codex_dir.strip():
        command.extend(("--codex-dir", str(resolve_codex_dir(codex_dir))))
    command.extend(("--lang", "en"))
    return command


def _capture_core_status(codex_dir: str) -> subprocess.CompletedProcess[str]:
    return _run_core_command(
        _targeted_core_args(codex_dir, "--status"),
        capture=True,
    )


def _capture_core_reconcile(codex_dir: str) -> subprocess.CompletedProcess[str]:
    return _run_core_command(
        _targeted_core_args(codex_dir, "--reconcile-managed-state"),
        capture=True,
    )


def inspect_reconciled_status(codex_dir: str) -> tuple[str, ManagedStatus]:
    """Separate active deployment health from uninstall/rollback readiness."""
    managed = inspect_managed_status(codex_dir)
    core_result = _capture_core_status(codex_dir)
    core_output = core_result.stdout or ""
    status = classify_deployment_status(core_output, core_result.returncode)
    notes: List[str] = []

    if codex_dir.strip() and status == "conflict" and managed.status == "deployed":
        reconciled = _capture_core_reconcile(codex_dir)
        reconcile_output = reconciled.stdout or ""
        if reconcile_output:
            notes.append(reconcile_output.strip())
        if reconciled.returncode == 0:
            verified = _capture_core_status(codex_dir)
            core_output = verified.stdout or ""
            status = classify_deployment_status(core_output, verified.returncode)
            managed = inspect_managed_status(codex_dir)

    if status == "conflict" and managed.status == "deployed":
        status = "degraded"
        managed.rollback_issues.append(
            "提示词仍在正常生效，但部署核心无法自动维护回滚或所有权证据"
        )
        managed.details.append("可使用“重建回滚基线”生成新的完整部署记录")
    else:
        managed.status = status
        if status == "conflict" and not managed.issues:
            managed.issues.append("部署核心检测到部署清单或所有权冲突")
    managed.status = status
    managed.details.extend(note for note in notes if note)
    combined_output = core_output
    if notes:
        combined_output += "\n" + "\n".join(f"[GUI] {note}" for note in notes if note) + "\n"
    combined_output += "\n" + managed.summary_text()
    return combined_output, managed


def is_managed_content_conflict(status: ManagedStatus) -> bool:
    return status.is_managed_conflict


def repair_config_conflict(
    codex_dir: str,
    language: str,
    md_file: str = "",
    md_name: str = "gpt-unrestricted",
    skip_hooks: bool = False,
) -> int:
    target = resolve_codex_dir(codex_dir)
    config_path = target / "config.toml"
    manifest_path = target / MANIFEST_FILENAME

    if not target.is_dir():
        print(f"[修复失败] Codex 目录不存在: {target}")
        return 1

    managed = inspect_managed_status(str(target))
    if managed.status == "recovery":
        print("[修复失败] 发现事务残留，请先执行事务恢复。")
        print(managed.summary_text(), end="")
        return 1

    deploy_args = ["--codex-dir", str(target), "--yes", "--lang", language]
    if md_file:
        deploy_args.extend(("--file", md_file))
    if md_name:
        deploy_args.extend(("--name", md_name))
    if skip_hooks:
        deploy_args.append("--skip-hooks-isolation")

    if managed.status == "deployed":
        print("[检查] 托管提示词正常，继续核对卸载与回滚证据。")
        core_result = _capture_core_status(str(target))
        core_output = core_result.stdout or ""
        core_status = classify_deployment_status(core_output, core_result.returncode)
        if core_status == "conflict":
            reconciled = _capture_core_reconcile(str(target))
            reconcile_output = reconciled.stdout or ""
            if reconcile_output:
                print(reconcile_output, end="" if reconcile_output.endswith("\n") else "\n")
            if reconciled.returncode == 0:
                core_result = _capture_core_status(str(target))
                core_output = core_result.stdout or ""
                core_status = classify_deployment_status(
                    core_output,
                    core_result.returncode,
                )
        if core_status == "deployed":
            print("[完成] 部署内容和回滚证据均正常，无需重新部署。")
            return 0
        if core_status in {"failed", "recovery"}:
            print("[修复失败] 部署核心状态检查失败或存在待恢复事务，已停止重建。")
            print(core_output, end="" if core_output.endswith("\n") else "\n")
            return 1
        print("[重建] 当前提示词仍然有效，但旧回滚证据无法完整恢复。")
        print("[重建] 将保留当前配置副本，并以当前状态建立新的部署基线。")

    if managed.status == "not_deployed":
        if not _is_regular_file(config_path):
            print("[修复失败] 当前未部署，但缺少可安全读取的 config.toml。")
            print(managed.summary_text(), end="")
            return 1
        print("[状态变化] 当前已是未部署状态，自动转为普通部署。")
        print(managed.summary_text(), end="")
        result = _run_core_command(deploy_args)
        if result.returncode == 0:
            print("[完成] 已使用 v0.1.3 部署核心完成部署。")
        return result.returncode

    if managed.status not in {"conflict", "deployed"}:
        print("[修复失败] 当前状态无法自动修复，请使用高级操作查看详情。")
        print(managed.summary_text(), end="")
        return 1

    if not _is_regular_file(config_path) or not _is_regular_file(manifest_path):
        print("[修复失败] config.toml 或部署清单不是可安全处理的普通文件。")
        print(managed.summary_text(), end="")
        return 1

    print("[检查] 仅校验本工具托管内容（忽略其他 config.toml 字段变化）")
    print(managed.summary_text(), end="")

    result = _run_core_command(["--repair-redeploy", *deploy_args])
    if result.returncode == 0:
        print("[完成] 配置冲突已修复，并已由 v0.1.3 部署核心重新部署。")
        return 0
    return result.returncode or 1


def run_conflict_repair_cli(argv: List[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--codex-dir", required=True)
    parser.add_argument("--lang", choices=("auto", "zh-CN", "en"), default="zh-CN")
    parser.add_argument("--file", default="")
    parser.add_argument("--name", default="gpt-unrestricted")
    parser.add_argument("--skip-hooks-isolation", action="store_true")
    args = parser.parse_args(argv)
    return repair_config_conflict(
        args.codex_dir,
        args.lang,
        args.file,
        args.name,
        args.skip_hooks_isolation,
    )


def classify_deployment_status(output: str, exit_code: int) -> str:
    """Map the stable English fields from the core status command to the GUI."""
    residue_prefix = "Transaction residue:"
    residue_lines = [
        line.strip() for line in output.splitlines() if line.strip().startswith(residue_prefix)
    ]
    if not residue_lines:
        return "failed"
    if any(line != f"{residue_prefix} none" for line in residue_lines):
        return "recovery"

    has_manifest = "deployment manifest: regular file" in output
    if "Deployability: blocked" in output:
        return "conflict" if has_manifest else "blocked"
    if exit_code != 0:
        return "failed"
    return "deployed" if has_manifest else "not_deployed"


class KeysmithGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.process: Optional[subprocess.Popen[str]] = None
        self.status_process: Optional[subprocess.Popen[str]] = None
        self.status_check_active = False
        self.status_refresh_pending = False
        self.current_deployment_status = "checking"
        self.current_status_output = ""
        self.current_managed_status: Optional[ManagedStatus] = None
        self.output_queue: "queue.Queue[tuple[str, Optional[int]]]" = queue.Queue()
        self.status_queue: "queue.Queue[tuple[str, ManagedStatus]]" = queue.Queue()

        self.operation = tk.StringVar(value=next(iter(OPERATIONS)))
        self.codex_dir = tk.StringVar(value=self._default_codex_dir())
        self.md_file = tk.StringVar()
        self.md_name = tk.StringVar(value="gpt-unrestricted")
        self.language = tk.StringVar(value="zh-CN")
        self.skip_hooks = tk.BooleanVar(value=False)
        self.command_preview = tk.StringVar()
        runtime = (
            "Windows 单文件版"
            if FROZEN
            else f"Python {sys.version.split()[0]}"
        )
        self.runtime_text = tk.StringVar(value=runtime)
        self.status_text = tk.StringVar(value="就绪")
        self.status_badge: Optional[tk.Label] = None
        self.status_refresh_button: Optional[ttk.Button] = None
        self.repair_button: Optional[tk.Button] = None
        self.advanced_frame: Optional[tk.Frame] = None
        self.advanced_toggle_button: Optional[tk.Button] = None
        self.advanced_expanded = False
        self.ui_font = self._pick_font(
            "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI"
        )
        self.code_font = self._pick_font("Cascadia Mono", "Consolas", "Courier New")

        self._configure_window()
        self._build_styles()
        self._build_ui()
        self._bind_updates()
        self._update_controls()
        self.root.after(100, self._drain_output_queue)
        self.root.after(100, self._drain_status_queue)
        self.root.after(250, self._refresh_status)

    @staticmethod
    def _default_codex_dir() -> str:
        candidates = []
        codex_home = os.environ.get("CODEX_HOME", "").strip()
        if codex_home:
            candidates.append(Path(codex_home).expanduser())
        candidates.append(Path.home() / ".codex")
        return next((str(path) for path in candidates if path.is_dir()), "")

    def _configure_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1040x820")
        self.root.minsize(900, 700)
        self.root.configure(bg="#F3F3F3")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _pick_font(self, *candidates: str) -> str:
        available = set(tkfont.families(self.root))
        return next((font for font in candidates if font in available), candidates[-1])

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Page.TFrame", background="#F3F3F3")
        style.configure("Surface.TFrame", background="#FFFFFF")
        style.configure("Header.TFrame", background="#F3F3F3")
        style.configure(
            "Title.TLabel",
            background="#F3F3F3",
            foreground="#1B1B1B",
            font=(self.ui_font, 19, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#F3F3F3",
            foreground="#616161",
            font=(self.ui_font, 9),
        )
        style.configure(
            "SurfaceTitle.TLabel",
            background="#FFFFFF",
            foreground="#1B1B1B",
            font=(self.ui_font, 11, "bold"),
        )
        style.configure(
            "Surface.TLabel",
            background="#FFFFFF",
            foreground="#242424",
            font=(self.ui_font, 9),
        )
        style.configure(
            "SurfaceMuted.TLabel",
            background="#FFFFFF",
            foreground="#616161",
            font=(self.ui_font, 9),
        )
        style.configure("TButton", font=(self.ui_font, 9), padding=(12, 7))
        style.configure("Compact.TButton", font=(self.ui_font, 9), padding=(9, 5))
        style.configure("Run.TButton", font=(self.ui_font, 9, "bold"), padding=(15, 8))
        style.configure("Field.TEntry", padding=(7, 6))
        style.configure("Field.TCombobox", padding=(5, 5))
        style.configure(
            "Surface.TCheckbutton",
            background="#FFFFFF",
            foreground="#242424",
            font=(self.ui_font, 9),
        )
        style.map("Surface.TCheckbutton", background=[("active", "#FFFFFF")])
        self.root.option_add("*TCombobox*Listbox.font", (self.ui_font, 9))

    def _build_ui(self) -> None:
        page = ttk.Frame(self.root, padding=(22, 18, 22, 16), style="Page.TFrame")
        page.pack(fill=tk.BOTH, expand=True)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(5, weight=1)

        header = ttk.Frame(page, style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.columnconfigure(1, weight=1)
        brand = tk.Label(
            header,
            text="5.6",
            width=4,
            height=2,
            background="#0067C0",
            foreground="#FFFFFF",
            font=(self.ui_font, 12, "bold"),
        )
        brand.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
        ttk.Label(header, text="coedx 5.6 破甲", style="Title.TLabel").grid(
            row=0, column=1, sticky="sw"
        )
        runtime_row = ttk.Frame(header, style="Header.TFrame")
        runtime_row.grid(row=1, column=1, sticky="nw", pady=(2, 0))
        ttk.Label(runtime_row, text="部署控制中心", style="Subtitle.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Label(runtime_row, text="  |  ", style="Subtitle.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            runtime_row, textvariable=self.runtime_text, style="Subtitle.TLabel"
        ).pack(side=tk.LEFT)

        status_row = ttk.Frame(header, style="Header.TFrame")
        status_row.grid(row=0, column=2, rowspan=2, sticky="e")
        ttk.Label(status_row, text="当前状态", style="Subtitle.TLabel").pack(
            side=tk.LEFT, padx=(0, 8)
        )
        self.status_badge = tk.Label(
            status_row,
            text="检查中...",
            background="#E8F1FB",
            foreground="#005A9E",
            padx=11,
            pady=5,
            font=(self.ui_font, 9, "bold"),
        )
        self.status_badge.pack(side=tk.LEFT, padx=(0, 8))
        self.status_refresh_button = ttk.Button(
            status_row,
            text="刷新",
            command=self._refresh_status,
            style="Compact.TButton",
        )
        self.status_refresh_button.pack(side=tk.LEFT)

        notice = tk.Frame(
            page,
            background="#E8F1FB",
            highlightbackground="#C7E0F4",
            highlightthickness=1,
        )
        notice.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        tk.Frame(notice, width=4, background="#0067C0").pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(
            notice,
            text="完成操作后重启 Codex，新的配置才会生效。",
            background="#E8F1FB",
            foreground="#004578",
            anchor="w",
            padx=12,
            pady=9,
            font=(self.ui_font, 9),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        primary = tk.Frame(
            page,
            background="#FFFFFF",
            highlightbackground="#DADADA",
            highlightthickness=1,
            padx=16,
            pady=14,
        )
        primary.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        primary.columnconfigure(0, weight=1, uniform="primary-actions")
        primary.columnconfigure(1, weight=1, uniform="primary-actions")
        ttk.Label(primary, text="快速操作", style="SurfaceTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        self.quick_deploy_button = tk.Button(
            primary,
            text="一键部署",
            command=self._quick_deploy,
            background="#0067C0",
            activebackground="#005A9E",
            foreground="#FFFFFF",
            activeforeground="#FFFFFF",
            disabledforeground="#D0D0D0",
            relief=tk.FLAT,
            borderwidth=0,
            font=(self.ui_font, 11, "bold"),
            height=2,
            cursor="hand2",
        )
        self.quick_deploy_button.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        self.quick_uninstall_button = tk.Button(
            primary,
            text="一键卸载",
            command=self._quick_uninstall,
            background="#F3F3F3",
            activebackground="#FDE7E9",
            foreground="#A4262C",
            activeforeground="#A4262C",
            disabledforeground="#A0A0A0",
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            font=(self.ui_font, 11, "bold"),
            height=2,
            cursor="hand2",
        )
        self.quick_uninstall_button.grid(row=1, column=1, sticky="ew", padx=(6, 0))
        repair_slot = tk.Frame(primary, background="#FFFFFF")
        repair_slot.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.repair_button = tk.Button(
            repair_slot,
            text="重新部署",
            command=self._redeploy_or_repair,
            background="#FFF4CE",
            activebackground="#FCE8A6",
            foreground="#7A5D00",
            activeforeground="#7A5D00",
            relief=tk.FLAT,
            borderwidth=0,
            highlightbackground="#E6C95C",
            highlightthickness=1,
            font=(self.ui_font, 9, "bold"),
            pady=7,
            cursor="hand2",
        )

        self.advanced_toggle_button = tk.Button(
            page,
            text="高级设置  >",
            command=self._toggle_advanced,
            anchor="w",
            background="#FFFFFF",
            activebackground="#F8F8F8",
            foreground="#242424",
            activeforeground="#242424",
            relief=tk.FLAT,
            borderwidth=0,
            highlightbackground="#DADADA",
            highlightthickness=1,
            font=(self.ui_font, 10, "bold"),
            padx=15,
            pady=9,
            cursor="hand2",
        )
        self.advanced_toggle_button.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        config = tk.Frame(
            page,
            background="#FFFFFF",
            highlightbackground="#DADADA",
            highlightthickness=1,
            padx=16,
            pady=12,
        )
        config.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        config.columnconfigure(1, weight=1)
        self.advanced_frame = config

        ttk.Label(config, text="操作", style="Surface.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=4
        )
        self.operation_box = ttk.Combobox(
            config,
            textvariable=self.operation,
            values=list(OPERATIONS),
            state="readonly",
            style="Field.TCombobox",
        )
        self.operation_box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(config, text="Codex 目录", style="Surface.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=4
        )
        self.codex_entry = ttk.Entry(
            config, textvariable=self.codex_dir, style="Field.TEntry"
        )
        self.codex_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self.codex_button = ttk.Button(
            config, text="浏览", command=self._browse_codex_dir, style="Compact.TButton"
        )
        self.codex_button.grid(row=1, column=2, padx=(8, 0), pady=4)

        ttk.Label(config, text="外部 MD", style="Surface.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=4
        )
        self.file_entry = ttk.Entry(
            config, textvariable=self.md_file, style="Field.TEntry"
        )
        self.file_entry.grid(row=2, column=1, sticky="ew", pady=4)
        self.file_button = ttk.Button(
            config, text="浏览", command=self._browse_md_file, style="Compact.TButton"
        )
        self.file_button.grid(row=2, column=2, padx=(8, 0), pady=4)

        ttk.Label(config, text="MD 文件名", style="Surface.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 12), pady=4
        )
        self.name_entry = ttk.Entry(
            config, textvariable=self.md_name, style="Field.TEntry"
        )
        self.name_entry.grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(config, text="不含 .md", style="SurfaceMuted.TLabel").grid(
            row=3, column=2, sticky="w", padx=(8, 0), pady=4
        )

        options = ttk.Frame(config, style="Surface.TFrame")
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(7, 4))
        ttk.Label(options, text="输出语言", style="Surface.TLabel").pack(
            side=tk.LEFT, padx=(0, 8)
        )
        self.language_box = ttk.Combobox(
            options,
            textvariable=self.language,
            values=("auto", "zh-CN", "en"),
            width=10,
            state="readonly",
            style="Field.TCombobox",
        )
        self.language_box.pack(side=tk.LEFT)
        self.hooks_check = ttk.Checkbutton(
            options,
            text="保持 hooks.json 活跃",
            variable=self.skip_hooks,
            style="Surface.TCheckbutton",
        )
        self.hooks_check.pack(side=tk.LEFT, padx=(22, 0))

        ttk.Label(config, text="命令预览", style="Surface.TLabel").grid(
            row=5, column=0, sticky="w", padx=(0, 12), pady=(5, 4)
        )
        command_entry = ttk.Entry(
            config,
            textvariable=self.command_preview,
            state="readonly",
            style="Field.TEntry",
        )
        command_entry.grid(row=5, column=1, sticky="ew", pady=(5, 4))
        self.run_button = ttk.Button(
            config,
            text="执行",
            command=self._run,
            style="Run.TButton",
        )
        self.run_button.grid(row=5, column=2, padx=(8, 0), pady=(5, 4))
        config.grid_remove()

        output_panel = tk.Frame(
            page,
            background="#FFFFFF",
            highlightbackground="#DADADA",
            highlightthickness=1,
            padx=14,
            pady=12,
        )
        output_panel.grid(row=5, column=0, sticky="nsew")
        output_panel.columnconfigure(0, weight=1)
        output_panel.rowconfigure(1, weight=1)
        output_header = ttk.Frame(output_panel, style="Surface.TFrame")
        output_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        output_header.columnconfigure(0, weight=1)
        ttk.Label(output_header, text="运行输出", style="SurfaceTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            output_header,
            text="清空",
            command=self._clear_output,
            style="Compact.TButton",
        ).grid(row=0, column=1)
        self.output = ScrolledText(
            output_panel,
            wrap=tk.WORD,
            height=9,
            font=(self.code_font, 9),
            background="#0C0C0C",
            foreground="#F2F2F2",
            insertbackground="#FFFFFF",
            selectbackground="#264F78",
            relief=tk.FLAT,
            borderwidth=0,
            padx=12,
            pady=10,
        )
        self.output.grid(row=1, column=0, sticky="nsew")
        self.output.configure(state=tk.DISABLED)

        footer = ttk.Frame(page, style="Page.TFrame")
        footer.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_text, style="Subtitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.stop_button = ttk.Button(
            footer,
            text="停止",
            command=self._stop_process,
            state=tk.DISABLED,
            style="Compact.TButton",
        )
        self.stop_button.grid(row=0, column=1)

    def _toggle_advanced(self) -> None:
        if self.advanced_frame is None or self.advanced_toggle_button is None:
            return
        self.advanced_expanded = not self.advanced_expanded
        if self.advanced_expanded:
            self.root.minsize(900, 860)
            if self.root.winfo_height() < 860:
                self.root.geometry(f"{max(self.root.winfo_width(), 900)}x860")
            self.advanced_frame.grid()
            self.advanced_toggle_button.configure(text="高级设置  v")
        else:
            self.advanced_frame.grid_remove()
            self.root.minsize(900, 700)
            self.advanced_toggle_button.configure(text="高级设置  >")

    def _bind_updates(self) -> None:
        self.operation.trace_add("write", lambda *_: self._update_controls())
        for variable in (
            self.codex_dir,
            self.md_file,
            self.md_name,
            self.language,
            self.skip_hooks,
        ):
            variable.trace_add("write", lambda *_: self._refresh_command_preview())

    def _operation_code(self) -> str:
        return OPERATIONS[self.operation.get()]

    def _update_controls(self) -> None:
        operation = self._operation_code()
        deploy = operation in DEPLOY_OPERATIONS
        has_codex_dir = operation in CODEX_DIR_OPERATIONS

        self._set_widget_state(self.codex_entry, tk.NORMAL if has_codex_dir else tk.DISABLED)
        self._set_widget_state(self.codex_button, tk.NORMAL if has_codex_dir else tk.DISABLED)
        self._set_widget_state(self.file_entry, tk.NORMAL if deploy else tk.DISABLED)
        self._set_widget_state(self.file_button, tk.NORMAL if deploy else tk.DISABLED)
        self._set_widget_state(self.name_entry, tk.NORMAL if deploy else tk.DISABLED)
        self._set_widget_state(self.hooks_check, tk.NORMAL if deploy else tk.DISABLED)
        self._refresh_command_preview()

    @staticmethod
    def _set_widget_state(widget: tk.Widget, state: str) -> None:
        widget.configure(state=state)

    def _build_command(self) -> List[str]:
        operation = self._operation_code()
        command = core_command_prefix()

        if operation == "help":
            command.append("--help")
            return command
        if operation == "version":
            command.append("--version")
            return command

        if operation == "deploy_preview":
            command.append("--dry-run")
        elif operation == "deploy":
            command.append("--yes")
        elif operation == "status":
            command.append("--status")
        elif operation in {"uninstall_preview", "uninstall"}:
            command.append("--uninstall")
            if operation == "uninstall":
                command.append("--yes")
        elif operation in {"recover_preview", "recover"}:
            command.append("--recover")
            if operation == "recover":
                command.append("--yes")
        elif operation == "restore_hooks":
            command.append("--restore-hooks")

        codex_dir = self.codex_dir.get().strip()
        if operation in CODEX_DIR_OPERATIONS and codex_dir:
            command.extend(("--codex-dir", codex_dir))

        if operation in DEPLOY_OPERATIONS:
            md_file = self.md_file.get().strip()
            md_name = self.md_name.get().strip()
            if md_file:
                command.extend(("--file", md_file))
            if md_name:
                command.extend(("--name", md_name))
            if self.skip_hooks.get():
                command.append("--skip-hooks-isolation")

        command.extend(("--lang", self.language.get()))
        return command

    def _refresh_command_preview(self) -> None:
        self.command_preview.set(format_command(self._build_command()))

    def _validate(self) -> bool:
        operation = self._operation_code()
        if not CORE_SCRIPT.is_file():
            messagebox.showerror("缺少部署核心文件", f"未找到：\n{CORE_SCRIPT}")
            return False

        codex_dir = self.codex_dir.get().strip()
        if operation in CODEX_DIR_OPERATIONS and codex_dir:
            path = Path(os.path.expandvars(os.path.expanduser(codex_dir)))
            if not path.is_dir():
                messagebox.showerror("目录不存在", f"Codex 目录不存在：\n{path}")
                return False

        if operation in DEPLOY_OPERATIONS:
            md_file = self.md_file.get().strip()
            if md_file and not Path(md_file).expanduser().is_file():
                messagebox.showerror("文件不存在", f"外部 MD 文件不存在：\n{md_file}")
                return False
            if self.skip_hooks.get() and not codex_dir:
                messagebox.showerror(
                    "缺少目录",
                    "保持 hooks.json 活跃时，必须明确指定 Codex 目录。",
                )
                return False
        return True

    def _set_operation_code(self, code: str) -> None:
        label = next(label for label, value in OPERATIONS.items() if value == code)
        self.operation.set(label)

    def _set_quick_action_state(self, state: str) -> None:
        self.quick_deploy_button.configure(state=state)
        self.quick_uninstall_button.configure(state=state)
        if self.repair_button is not None:
            self.repair_button.configure(state=state)

    def _quick_deploy(self) -> None:
        confirmed = messagebox.askyesno(
            "确认一键部署",
            "将使用当前配置执行部署。是否继续？",
        )
        if confirmed:
            self._set_operation_code("deploy")
            self._run()

    def _quick_uninstall(self) -> None:
        confirmed = messagebox.askyesno(
            "确认一键卸载",
            "将按部署清单恢复并卸载当前部署。是否继续？",
        )
        if confirmed:
            self._set_operation_code("uninstall")
            self._run()

    def _run(self) -> None:
        if self.process is not None or not self._validate():
            return
        self._start_command(self._build_command())

    def _start_command(self, command: List[str]) -> None:
        self._clear_output()
        self._append_output(f"> {format_command(command)}\n\n")
        self.status_text.set("正在运行...")
        self.run_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self._set_quick_action_state(tk.DISABLED)
        threading.Thread(target=self._worker, args=(command,), daemon=True).start()

    def _worker(self, command: List[str]) -> None:
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.output_queue.put((line, None))
            exit_code = self.process.wait()
            self.output_queue.put(("", exit_code))
        except OSError as exc:
            self.output_queue.put((f"启动失败：{exc}\n", -1))

    def _drain_output_queue(self) -> None:
        try:
            while True:
                text, exit_code = self.output_queue.get_nowait()
                if text:
                    self._append_output(text)
                if exit_code is not None:
                    self.process = None
                    self.run_button.configure(state=tk.NORMAL)
                    self.stop_button.configure(state=tk.DISABLED)
                    self._set_quick_action_state(tk.NORMAL)
                    self.status_text.set(f"运行结束，退出码：{exit_code}")
                    self._append_output(f"\n[GUI] 进程已结束，退出码：{exit_code}\n")
                    self._refresh_status()
        except queue.Empty:
            pass
        self.root.after(100, self._drain_output_queue)

    def _set_deployment_status(
        self,
        text: str,
        background: str,
        foreground: str,
    ) -> None:
        if self.status_badge is not None:
            self.status_badge.configure(
                text=text,
                background=background,
                foreground=foreground,
            )

    def _refresh_status(self) -> None:
        if self.status_check_active:
            self.status_refresh_pending = True
            return
        self._apply_deployment_status("checking")
        if self.status_refresh_button is not None:
            self.status_refresh_button.configure(state=tk.DISABLED)
        self.status_check_active = True
        codex_dir = self.codex_dir.get().strip()
        threading.Thread(
            target=self._status_worker,
            args=(codex_dir,),
            daemon=True,
        ).start()

    def _status_worker(self, codex_dir: str) -> None:
        output, managed = inspect_reconciled_status(codex_dir)
        self.status_queue.put((output, managed))

    def _drain_status_queue(self) -> None:
        try:
            while True:
                output, managed = self.status_queue.get_nowait()
                self.status_process = None
                self.status_check_active = False
                if self.status_refresh_button is not None:
                    self.status_refresh_button.configure(state=tk.NORMAL)
                status = managed.status
                self.current_deployment_status = status
                self.current_status_output = output
                self.current_managed_status = managed
                self._apply_deployment_status(status)
                self._update_repair_button(status, managed)
                if self.status_refresh_pending:
                    self.status_refresh_pending = False
                    self.root.after(0, self._refresh_status)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_status_queue)

    def _apply_deployment_status(self, status: str) -> None:
        self._set_deployment_status(*STATUS_STYLES[status])

    def _update_repair_button(self, status: str, managed: ManagedStatus) -> None:
        if self.repair_button is None:
            return
        managed_conflict = status == "conflict" and is_managed_content_conflict(managed)
        rollback_degraded = status == "degraded"
        visible = status in {"deployed", "conflict", "degraded"}
        if visible:
            if rollback_degraded:
                button_text = "重建回滚基线"
            elif managed_conflict:
                button_text = "修复配置冲突并重新部署"
            else:
                button_text = "重新部署"
            warning_action = managed_conflict or rollback_degraded
            self.repair_button.configure(
                text=button_text,
                background=("#FFF4CE" if warning_action else "#E8F1FB"),
                activebackground=("#FCE8A6" if warning_action else "#D5E8F7"),
                foreground=("#7A5D00" if warning_action else "#005A9E"),
                activeforeground=("#7A5D00" if warning_action else "#005A9E"),
                highlightbackground=("#E6C95C" if warning_action else "#8ABBDD"),
            )
            if not self.repair_button.winfo_manager():
                self.repair_button.pack(fill=tk.X, pady=(10, 0))
        elif self.repair_button.winfo_manager():
            self.repair_button.pack_forget()

    def _redeploy_or_repair(self) -> None:
        managed = self.current_managed_status
        if (
            self.current_deployment_status in {"conflict", "degraded"}
            and managed is not None
        ):
            self._repair_and_redeploy()
            return

        confirmed = messagebox.askyesno(
            "确认重新部署",
            "将使用当前配置重新部署，并保留核心脚本生成的备份。是否继续？",
        )
        if confirmed:
            self._set_operation_code("deploy")
            self._run()

    def _build_repair_command(self) -> List[str]:
        if FROZEN:
            command = [sys.executable, REPAIR_CLI_FLAG]
        else:
            command = [sys.executable, str(Path(__file__).resolve()), REPAIR_CLI_FLAG]
        command.extend(("--codex-dir", self.codex_dir.get().strip()))
        command.extend(("--lang", self.language.get()))
        md_file = self.md_file.get().strip()
        md_name = self.md_name.get().strip()
        if md_file:
            command.extend(("--file", md_file))
        if md_name:
            command.extend(("--name", md_name))
        if self.skip_hooks.get():
            command.append("--skip-hooks-isolation")
        return command

    def _repair_and_redeploy(self) -> None:
        if self.process is not None:
            return
        codex_dir = self.codex_dir.get().strip()
        if not codex_dir or not Path(codex_dir).expanduser().is_dir():
            messagebox.showerror("目录不存在", "请先选择发生冲突的 Codex 配置目录。")
            return
        if self.current_deployment_status == "degraded":
            title = "重建回滚基线"
            message = (
                "当前提示词仍然正常生效，但原始回滚备份不完整。\n\n"
                "继续后会备份当前配置和旧清单，并以当前状态建立新的回滚基线。"
                "原先已经丢失的旧内容不会被恢复。是否继续？"
            )
        else:
            title = "修复配置冲突"
            message = (
                "将备份当前配置和旧部署清单，然后使用 v0.1.3 部署核心重新部署。\n\n"
                "现有模型、MCP 和插件配置会保留。是否继续？"
            )
        confirmed = messagebox.askyesno(title, message)
        if confirmed:
            self._start_command(self._build_repair_command())

    def _stop_process(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.status_text.set("正在停止...")

    def _append_output(self, text: str) -> None:
        for term in DISPLAY_HIDDEN_TERMS:
            text = text.replace(term, "")
        self.output.configure(state=tk.NORMAL)
        self.output.insert(tk.END, text)
        self.output.see(tk.END)
        self.output.configure(state=tk.DISABLED)

    def _clear_output(self) -> None:
        self.output.configure(state=tk.NORMAL)
        self.output.delete("1.0", tk.END)
        self.output.configure(state=tk.DISABLED)

    def _browse_codex_dir(self) -> None:
        selected = filedialog.askdirectory(
            title="选择 Codex 配置目录",
            initialdir=self.codex_dir.get() or str(Path.home()),
        )
        if selected:
            self.codex_dir.set(selected)

    def _browse_md_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 Markdown 指令文件",
            initialdir=str(BASE_DIR),
            filetypes=(("Markdown 文件", "*.md"), ("所有文件", "*.*")),
        )
        if selected:
            self.md_file.set(selected)

    def _on_close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            close = messagebox.askyesno("命令仍在运行", "停止当前命令并关闭窗口？")
            if not close:
                return
            self.process.terminate()
        self.root.destroy()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == CORE_CLI_FLAG:
        run_bundled_core()
        return
    if len(sys.argv) > 1 and sys.argv[1] == REPAIR_CLI_FLAG:
        raise SystemExit(run_conflict_repair_cli(sys.argv[2:]))
    try:
        require_supported_python()
    except RuntimeError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_TITLE, str(exc))
        root.destroy()
        raise SystemExit(1)

    root = tk.Tk()
    KeysmithGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
