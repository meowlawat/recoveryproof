"""Exit when the test app crashes so Docker's restart policy can act."""
import os
import subprocess
import sys

proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "target:app", "--host", "0.0.0.0", "--port", "8001"])
with open("/tmp/recoveryproof-app.pid", "w") as pid_file:
    pid_file.write(str(proc.pid))
code = proc.wait()
sys.exit(code if code >= 0 else 128 - code)
