"""支持 python -m e2e_runner 直接执行。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
