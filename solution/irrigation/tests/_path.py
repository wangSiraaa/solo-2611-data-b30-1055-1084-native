import os
import sys

# 让测试既可用 `python3 -m unittest discover -s .../tests` 从仓库根运行，
# 也可在包目录内直接运行；测试模块以顶层名 hydraulics/... 导入。
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
