"""Stable absolute launcher used by the installed local plugin."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factory.mcp_server import main

if __name__ == '__main__':
    main()
