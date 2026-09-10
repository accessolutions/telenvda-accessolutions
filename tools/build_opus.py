"""Build the pinned libopus release as the two add-on DLLs."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


VERSION = "1.6.1"
SOURCE_URL = f"https://downloads.xiph.org/releases/opus/opus-{VERSION}.tar.gz"
SOURCE_SHA256 = "6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1"
ARCHITECTURES = ("x86", "x64")


def _run(command: list[str], cwd: Path | None = None) -> None:
	print("+", " ".join(command), flush=True)
	subprocess.run(command, cwd=cwd, check=True)


def _download(source: Path) -> None:
	print(f"Downloading {SOURCE_URL}", flush=True)
	with urllib.request.urlopen(SOURCE_URL) as response, source.open("wb") as destination:
		shutil.copyfileobj(response, destination)
	digest = hashlib.sha256(source.read_bytes()).hexdigest()
	if digest != SOURCE_SHA256:
		raise RuntimeError(f"Unexpected libopus SHA-256: {digest}")


def _cmake_generator() -> str:
	if generator := os.environ.get("CMAKE_GENERATOR"):
		return generator
	if os.name == "nt":
		return "Visual Studio 17 2022"
	return "Ninja" if shutil.which("ninja") else "Unix Makefiles"


def _configure_command(source: Path, build: Path, architecture: str) -> list[str]:
	command = [
		"cmake",
		"-S",
		str(source),
		"-B",
		str(build),
		"-G",
		_cmake_generator(),
		"-DOPUS_BUILD_SHARED_LIBRARY=ON",
		"-DOPUS_BUILD_PROGRAMS=OFF",
		"-DOPUS_BUILD_TESTING=OFF",
		"-DOPUS_ENABLE_FLOAT_API=ON",
		"-DOPUS_HARDENING=ON",
	]
	if os.name == "nt":
		command.extend(["-A", "Win32" if architecture == "x86" else "x64"])
		return command
	compiler = "i686-w64-mingw32-gcc" if architecture == "x86" else "x86_64-w64-mingw32-gcc"
	windres = "i686-w64-mingw32-windres" if architecture == "x86" else "x86_64-w64-mingw32-windres"
	command.extend([
		"-DCMAKE_SYSTEM_NAME=Windows",
		f"-DCMAKE_C_COMPILER={compiler}",
		f"-DCMAKE_RC_COMPILER={windres}",
		"-DCMAKE_C_FLAGS=-static-libgcc",
		"-DCMAKE_SHARED_LINKER_FLAGS=-static-libgcc -static-libssp",
	])
	return command


def _find_dll(directory: Path) -> Path:
	candidates = sorted(directory.rglob("opus.dll"))
	if not candidates:
		candidates = sorted(directory.rglob("libopus*.dll"))
	if not candidates:
		raise RuntimeError(f"CMake did not produce an Opus DLL under {directory}")
	return candidates[0]


def _build_one(source: Path, workspace: Path, destination: Path, architecture: str) -> None:
	build = workspace / f"build-{architecture}"
	install = workspace / f"install-{architecture}"
	_run(_configure_command(source, build, architecture))
	build_command = ["cmake", "--build", str(build), "--config", "Release", "--target", "opus"]
	_run(build_command)
	_run(["cmake", "--install", str(build), "--config", "Release", "--prefix", str(install)])
	destination.mkdir(parents=True, exist_ok=True)
	shutil.copy2(_find_dll(install), destination / "opus.dll")


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--output",
		type=Path,
		default=Path(__file__).resolve().parents[1] / "addon/globalPlugins/remoteClient/native",
		help="native directory receiving x86/opus.dll and x64/opus.dll",
	)
	args = parser.parse_args()
	if shutil.which("cmake") is None:
		raise SystemExit("cmake is required to build libopus")
	with tempfile.TemporaryDirectory(prefix="telenvda-opus-") as temporary:
		workspace = Path(temporary)
		archive = workspace / f"opus-{VERSION}.tar.gz"
		_download(archive)
		with tarfile.open(archive, "r:gz") as source_archive:
			source_archive.extractall(workspace, filter="data")
		source = workspace / f"opus-{VERSION}"
		for architecture in ARCHITECTURES:
			_build_one(source, workspace, args.output / architecture, architecture)
	print(f"Built libopus {VERSION} for x86 and x64 in {args.output}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
