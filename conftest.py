# conftest.py
# -*- coding: utf-8 -*-
"""
اطمینان از اینکه پوشه‌ی ریشه پروژه (هم‌سطح با app/) در sys.path هست
تا ایمپورت app.* در تست‌ها بدون خطا کار کند.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
