"""Command-line interface for validating and running chemistry action graphs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .dag import mermaid
from .graph import MockDriver, execute_graph, validate_graph
from .recipes import compile_recipe, recipe_names
from .registry import load_catalog, load_profile


def _read_graph(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("graph file must contain a JSON object")
    return document


def _dump(value: Any, output: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def _profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", help="profile JSON path (default: package profile.json)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m task_skills.chemistry.cli",
                                     description="Validate or dispatch strict chemistry action DAGs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("catalog", help="print the registered atomic skills")
    _profile_argument(catalog)

    plan = subparsers.add_parser("plan", help="compile and statically validate a built-in recipe")
    plan.add_argument("recipe", choices=recipe_names())
    _profile_argument(plan)

    validate = subparsers.add_parser("validate", help="validate a JSON graph without connecting to hardware")
    validate.add_argument("file")
    _profile_argument(validate)
    validate.add_argument("--output")

    dag = subparsers.add_parser("dag", help="render a validated graph as a mermaid flowchart")
    dag.add_argument("file")
    _profile_argument(dag)
    dag.add_argument("--output", help="also write the mermaid text to this path")

    run = subparsers.add_parser("run", help="execute a graph (MockDriver by default)")
    run.add_argument("file")
    run.add_argument("--live", action="store_true", help="explicitly import and use LiveDriver")
    _profile_argument(run)
    run.add_argument("--output", help="also save the execution report to this JSON path")

    home = subparsers.add_parser(
        "home", help="return both arms to the calibrated initial joint pose (live only)")
    home.add_argument("--live", action="store_true", help="explicitly import and use LiveDriver")
    home.add_argument("--arms", default="left,right",
                      help="comma-separated arms to home, in order (default: left,right)")
    home.add_argument("--timeout", type=float, default=None,
                      help="seconds to wait for each arm to arrive (default: 180)")
    _profile_argument(home)
    home.add_argument("--output", help="also save the execution report to this JSON path")

    return parser


def _confirm_home() -> bool:
    try:
        answer = input("Homing moves both arms to the calibrated initial joint pose. "
                       "Keep the workspace clear. Type YES to allow commands: ")
    except EOFError:
        print("no interactive input to confirm with; refusing to move the arms",
              file=sys.stderr)
        return False
    return answer.strip() == "YES"


def _home(arguments: argparse.Namespace) -> int:
    """Home the arms. This is the operator's reset action, not a registered atom."""
    if not arguments.live:
        print("error: homing commands the physical arms; re-run with --live and confirm on site",
              file=sys.stderr)
        return 2
    profile = load_profile(arguments.profile)
    from .driver import LiveDriver

    arms = [value.strip() for value in arguments.arms.split(",") if value.strip()]
    if not arms:
        print("error: --arms listed no arm to home", file=sys.stderr)
        return 2
    driver = LiveDriver(profile=profile, allow_live=True, confirmation=_confirm_home)
    try:
        report = driver.home(arms, profile, timeout=arguments.timeout)
    except Exception as error:  # noqa: BLE001 - the report has to carry the reason
        report = {"driver": "LiveDriver", "operation": "home", "arms": {},
                  "success": False, "failed_arms": arms,
                  "error": f"{type(error).__name__}: {error}"}
    try:
        report["close"] = driver.close()
    except Exception as error:  # noqa: BLE001 - teardown must not hide the result
        report["close_error"] = f"{type(error).__name__}: {error}"
    report["status"] = "completed_command_trace" if report.get("success") else "failed"
    _dump(report, arguments.output)
    return 0 if report.get("success") else 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "catalog":
            _dump(load_catalog())
            return 0

        if arguments.command == "home":
            return _home(arguments)

        profile = load_profile(arguments.profile)
        if arguments.command == "plan":
            normalized = validate_graph(compile_recipe(arguments.recipe), profile)
            _dump(normalized)
            return 0

        document = _read_graph(arguments.file)
        normalized = validate_graph(document, profile)
        if arguments.command == "validate":
            _dump(normalized, arguments.output)
            return 0

        if arguments.command == "dag":
            text = mermaid(normalized)
            if arguments.output:
                Path(arguments.output).write_text(text, encoding="utf-8")
            print(text, end="")
            return 0

        if arguments.live:
            # Keep driver import and construction after complete static validation.
            from .driver import LiveDriver

            def confirm_live() -> bool:
                return input("Live graph preflight has passed. Type YES to allow commands: ").strip() == "YES"

            driver = LiveDriver(profile=profile, allow_live=True, confirmation=confirm_live)
        else:
            driver = MockDriver()
        report = execute_graph(document, driver, profile=profile)
        _dump(report, arguments.output)
        return 0 if report.get("status") in {"simulated", "completed_command_trace"} else 1
    except (OSError, json.JSONDecodeError, ValueError, KeyError, ImportError) as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
