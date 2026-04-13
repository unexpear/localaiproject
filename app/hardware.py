import platform
import shutil
import subprocess
import sys

def get_os_info():
	return platform.platform()

def get_cpu_info():
	return platform.processor() or platform.machine()

def get_ram_info():
	try:
		import psutil
		return psutil.virtual_memory().total
	except ImportError:
		return None

def get_gpu_info():
	# Try nvidia-smi first
	try:
		result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True)
		if result.returncode == 0:
			return result.stdout.strip()
	except Exception:
		pass
	# Fallback: None
	return None

def get_disk_info():
	total, used, free = shutil.disk_usage(".")
	return {"total": total, "used": used, "free": free}

def summary():
	return {
		"os": get_os_info(),
		"cpu": get_cpu_info(),
		"ram": get_ram_info(),
		"gpu": get_gpu_info(),
		"disk": get_disk_info(),
		"python": sys.version,
	}

if __name__ == "__main__":
	import json
	print(json.dumps(summary(), indent=2))
