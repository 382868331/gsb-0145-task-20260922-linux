"""系统测试包：unittest discover -s tests 时把本目录加入 sys.path。"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
