"""Frozen wrapper for upstream OthrineTM pelta-control (GPL-3.0)."""
import sys
from pelta.cli import main
if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
