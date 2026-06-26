import subprocess
import time
import sys

print("Starting server...")
server_process = subprocess.Popen(
    [sys.executable, "server.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding='utf-8',
    errors='replace'  # Replace unencodable characters
)

# Wait for server to start
time.sleep(5)

print("\nRunning publish test...")
test_process = subprocess.Popen(
    [sys.executable, "test_publish.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding='utf-8',
    errors='replace'
)

# Capture test output
test_output, _ = test_process.communicate()
print("=== TEST OUTPUT ===")
print(test_output)

# Wait a bit for server to process
time.sleep(2)

# Kill server and capture its output
server_process.terminate()
server_output, _ = server_process.communicate(timeout=5)

print("\n=== SERVER OUTPUT (Last 2000 chars) ===")
print(server_output[-2000:])
