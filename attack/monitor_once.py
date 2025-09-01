import subprocess

result = subprocess.run(
            ['sudo', 'bash', './enhanced-node-detect.sh'],
            capture_output=True,
            text=True,
            timeout=30
        )

print(f"=== Return Code: {result.returncode} ===")
print(f"=== STDOUT ===")
print(result.stdout)
print(f"=== STDERR ===") 
print(result.stderr)