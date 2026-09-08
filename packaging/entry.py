"""PyInstaller entry; package-relative imports work from every installation path."""
from agent_manager.application import main

if __name__ == "__main__":
    raise SystemExit(main())
