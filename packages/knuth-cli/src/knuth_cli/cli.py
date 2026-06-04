import argparse
import sys

from rich.console import Console

from knuth_cli import __version__

console = Console()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knuth", description="Knuth agent framework")
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("run", help="Run an agent session (not implemented)")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "run":
        console.print("[yellow]run[/yellow] is not implemented yet")
        return 1
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
