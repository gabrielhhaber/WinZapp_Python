"""Windows on ARM64: the native ARM64 whisper.cpp builds, the CUDA 13 one as
an option (core/transcription/whisper_cpp_builds.py, machine_arch.py)."""
from core.transcription import machine_arch, whisper_cpp_builds as b


def test_the_machine_not_the_process_decides():
    assert machine_arch.is_arm64(0xAA64) is True
    assert machine_arch.is_arm64(0x8664) is False       # x64


def test_arm64_gets_the_native_builds_from_their_own_release():
    cpu, cuda = b.builds_for(arm64=True)
    assert (cpu.id, cuda.id) == ("cpu-arm64", "cuda-13.4-arm64")
    assert cpu.tag == cuda.tag == b.ARM64_RELEASE_TAG
    assert cuda.url.endswith("/b5454/whisper-bin-win-cuda-13.4-arm64.zip")
    assert not cpu.uses_cuda and cuda.uses_cuda
    for build in (cpu, cuda):
        names = [path for path, _digest in build.pinned_files]
        assert "Release/whisper-cli.exe" in names
        assert all(len(digest) == 64 for _path, digest in build.pinned_files)
    assert "Release/cudart64_13.dll" in [p for p, _d in cuda.pinned_files]


def test_x64_keeps_its_pinned_release():
    cpu, cuda = b.builds_for(arm64=False)
    assert (cpu.id, cuda.id) == ("cpu", "cuda-12.4")
    assert cpu.tag == cuda.tag == b.RELEASE_TAG


def test_cuda_13_is_offered_from_turing_and_to_blackwell():
    _cpu, arm_cuda = b.builds_for(arm64=True)
    assert b.cuda_build_supported((7, 5), arm_cuda) is True
    assert b.cuda_build_supported((12, 0), arm_cuda) is True
    assert b.cuda_build_supported((6, 1), arm_cuda) is False
    _cpu, x64_cuda = b.builds_for(arm64=False)
    assert b.cuda_build_supported((6, 1), x64_cuda) is True
    assert b.cuda_build_supported((12, 0), x64_cuda) is False
    assert b.cuda_build_supported(None, arm_cuda) is False
