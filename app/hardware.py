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

def _parse_nvidia_smi_memory(raw):
	"""Sum the per-GPU ``memory.total`` MiB values nvidia-smi prints
	(one integer per line under ``--format=csv,noheader,nounits``) into
	total bytes. Tolerant of a stray ``MiB`` suffix and multi-GPU
	output. Returns None when nothing parses."""
	if not raw:
		return None
	total_mib = 0
	found = False
	for line in raw.splitlines():
		token = line.strip().split(",")[0].strip()
		# Tolerate a unit suffix if --nounits wasn't honoured.
		for unit in ("MiB", "MIB", "Mb", "MB"):
			token = token.replace(unit, "")
		token = token.strip()
		if not token:
			continue
		try:
			total_mib += int(float(token))
			found = True
		except ValueError:
			continue
	if not found:
		return None
	return total_mib * 1024 * 1024

def get_vram_info():
	"""Total dedicated GPU VRAM in bytes across detected NVIDIA GPUs,
	or None when no NVIDIA GPU is detected / nvidia-smi is unavailable.

	Honest gaps: AMD (``rocm-smi``) and Apple unified memory are not
	probed yet and return None. The model-fit classifier degrades to
	RAM-only (its prior behaviour) when VRAM is unknown, so a missing
	probe never *breaks* fit classification — it just can't tell GPU
	from CPU placement on those backends."""
	try:
		result = subprocess.run(
			["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
			capture_output=True, text=True,
		)
	except Exception:
		return None
	if result.returncode != 0:
		return None
	return _parse_nvidia_smi_memory(result.stdout)

def get_disk_info():
	total, used, free = shutil.disk_usage(".")
	return {"total": total, "used": used, "free": free}

def summary():
	return {
		"os": get_os_info(),
		"cpu": get_cpu_info(),
		"ram": get_ram_info(),
		"gpu": get_gpu_info(),
		"vram": get_vram_info(),
		"disk": get_disk_info(),
		"python": sys.version,
	}

if __name__ == "__main__":
	import json
	print(json.dumps(summary(), indent=2))
