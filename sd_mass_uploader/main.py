"""Entry point: ``python main.py <command>``."""

import sys

if __name__ == "__main__":
    if sys.argv[1:3] == ["fixer", "guard"]:  # Claude Code's guard hook: fast, no config needed.
        from src.ai.claude_fixer import guard_main

        sys.exit(guard_main())
    from src.cli.commands import main

    sys.exit(main())
