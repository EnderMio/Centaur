from __future__ import annotations

import argparse
from importlib.resources import files
from pathlib import Path

from centaur import __version__
from centaur.engine import MEMORY_FILES, ROLE_ORDER, init_state_file, run_workflow

TEMPLATE_FILES = ("AGENTS.md", "SUPERVISOR.md", "WORKER.md", "VALIDATOR.md", "PROPOSAL.md")


def cmd_init(args: argparse.Namespace) -> int:
    target_dir = Path(args.path).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    template_dir = files("centaur.templates")
    written: list[str] = []
    skipped: list[str] = []

    for name in TEMPLATE_FILES:
        target_file = target_dir / name
        if target_file.exists() and not args.force:
            skipped.append(name)
            continue
        content = template_dir.joinpath(name).read_text(encoding="utf-8")
        target_file.write_text(content, encoding="utf-8")
        written.append(name)

    for name in MEMORY_FILES:
        target_file = target_dir / name
        if target_file.exists():
            skipped.append(name)
            continue
        target_file.touch()
        written.append(name)

    task_file = target_dir / "TASK.md"
    if task_file.exists():
        skipped.append("TASK.md")
    else:
        task_file.write_text("# 当前任务 (Task)\n", encoding="utf-8")
        written.append("TASK.md")

    if init_state_file(target_dir, force=args.force):
        written.append(".centaur_state.json")
    else:
        skipped.append(".centaur_state.json")

    print(f"✅ 已初始化: {target_dir}")
    if written:
        print("已创建/覆盖: " + ", ".join(written))
    if skipped:
        print("已存在(跳过): " + ", ".join(skipped))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    run_workflow(Path(args.path).resolve(), start_step=args.from_role)
    return 0


def cmd_version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="centaur", description="Centaur file-driven multi-agent workflow CLI.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Initialize Centaur markdown templates in a directory.")
    init_parser.add_argument("path", nargs="?", default=".", help="Target directory (default: current directory).")
    init_parser.add_argument("-f", "--force", action="store_true", help="Overwrite existing template markdown files.")
    init_parser.set_defaults(func=cmd_init)

    run_parser = subparsers.add_parser("run", help="Run the Supervisor -> Worker -> Validator workflow loop.")
    run_parser.add_argument("path", nargs="?", default=".", help="Workspace directory (default: current directory).")
    run_parser.add_argument(
        "--from-role",
        choices=ROLE_ORDER,
        help="Override resume state and force the next role for this workflow.",
    )
    run_parser.set_defaults(func=cmd_run)

    version_parser = subparsers.add_parser("version", help="Print Centaur CLI version.")
    version_parser.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
