import os
from pathlib import Path
import subprocess
import sys
import unittest


class StatusboardSuite(unittest.TestCase):
    def test_statusboard(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "statusboard"],
                       cwd=root, env=os.environ.copy(), check=True)
