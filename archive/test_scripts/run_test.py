import subprocess
import time
import sys
import os

# Set UTF-8 encoding for output
os.environ['PYTHONIOENCODING'] = 'utf-8'

print("[1/3] Starting server in background...")
server_process = subprocess.Popen(
    [sys.executable, "server.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding='utf-8',
    errors='replace',
    bufsize=1
)

# Wait for server startup
time.sleep(6)
print("[2/3] Server should be ready, running test...")

# Run test
result = subprocess.run(
    [sys.executable, "test_publish.py"],
    capture_output=True,
    text=True,
    encoding='utf-8',
    errors='replace'
)

print("\n=== TEST RESULT ===")
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)

# Give server time to log the error
time.sleep(1)

print("\n[3/3] Terminating server and capturing logs...")
server_process.terminate()

try:
    output, _ = server_process.communicate(timeout=3)
    # Find the error in output
    lines = output.split('\n')
    error_start = -1
    for i, line in enumerate(lines):
        if '500 Internal Server Error' in line or 'ERROR' in line or 'Exception' in line:
            error_start = max(0, i - 5)
            break
    
    if error_start >= 0:
        print("\n=== SERVER ERROR CONTEXT ===")
        print('\n'.join(lines[error_start:error_start+30]))
    else:
        print("\n=== LAST 50 LINES OF SERVER OUTPUT ===")
        print('\n'.join(lines[-50:]))
except subprocess.TimeoutExpired:
    server_process.kill()
    print("[ERROR] Server did not terminate gracefully")
