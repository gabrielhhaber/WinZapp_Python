"""Which whisper.cpp builds exist, which a machine is offered, and zip names.

The pinned release as data, and the pure decisions about it, apart from the
module that downloads and installs (whisper_cpp_runtime) so they can be read —
and tested — without a disk or a network:

* **One release, pinned whole, by digest.** Tag b4938; every zip below was
  measured on the release page and through the GitHub API, which agreed. The
  URL names the tag, never "latest", so a new release cannot swap the bytes
  under a digest that no longer describes them.

* **Two builds are offered, of the six published.** The CPU build
  (`whisper-bin-x64.zip`, 8 MB) always: it runs everywhere and is the
  fallback of every other answer. The CUDA 12.4 build (671 MB) only to an
  NVIDIA card whose compute capability is known and lies in [5.0, 12.0) —
  `cuda_build_supported()`. The ceiling is the reason this rule exists: both
  CUDA builds of this release predate Blackwell, and on sm_120 a build with no
  kernels for it fails at run time after the user waited through 671 MB; the
  floor is CUDA 12's own, which dropped Kepler. An unknown capability gets no
  offer, for the same reason device.gpu_supports_int8() refuses one: offering a
  download that may not run costs far more than not offering it. Not offered:
  CUDA 11.8 (270 MB). What it reaches that 12.4 does not is a driver too old
  for CUDA 12 — from before the end of 2022, where updating the driver is the
  better fix than a second GPU build — and Kepler cards, whose last driver
  branch is itself out of support; for that, it would double the GPU matrix
  this module has to keep honest. The OpenBLAS build
  (21 MB) is a second CPU build whose gain over ggml's own kernels was never
  measured here, and a choice the user cannot judge is not a feature. Win32
  builds: WinZapp is 64-bit. No Vulkan build is published for Windows.

* **The zip's layout is not known, so it is not assumed.** Whether the files
  sit in `Release/` or at the root, and whether this release still ships an
  old `main.exe` beside `whisper-cli.exe`, was not verified: the extraction
  keeps the zip's own tree and `locate_executable()` finds whisper-cli.exe
  wherever it is. A zip without one is refused (WHISPER_CPP_CORRUPTED) rather
  than run under another name — `main.exe`, where recent releases still ship
  one, is a deprecation stub, not the program.

No user-facing text lives here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RELEASE_TAG = "b4938"
_RELEASE_URL = "https://github.com/ggml-org/whisper.cpp/releases/download/{tag}/{name}"

EXECUTABLE_NAME = "whisper-cli.exe"
SERVER_NAME = "whisper-server.exe"

# The x64 CUDA 12.4 build: CUDA 12 has no Kepler (sm_3x) support, and this
# release's CUDA builds have no Blackwell (sm_120) kernels: [5.0, 12.0), the
# RuntimeBuild defaults. See the module docstring.

# Names Windows maps to a device whatever the folder or the extension: a file
# "nul.dll" written there goes nowhere, and "CON" opens the console.
_DOS_DEVICE = re.compile(
    r"^(?:con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?$", re.I
)


@dataclass(frozen=True)
class RuntimeBuild:
    """One published Windows build of the pinned release."""

    id: str
    archive: str
    archive_bytes: int
    archive_sha256: str
    uses_cuda: bool
    #: ((relative path, sha256), ...) of the files that run; () pins nothing.
    pinned_files: tuple = ()
    #: sha256 of the zip's whisper-server.exe, the program that keeps the model
    #: loaded (whisper_cpp_server); "" pins none, and the server is not used.
    #: Apart from `pinned_files` on purpose: those are what every run needs,
    #: and a folder without the server (a reused one may lack it) still runs.
    server_sha256: str = ""
    #: The release this build's zip comes from (the ARM64 builds are a later
    #: one: the pinned x64 release has none).
    tag: str = RELEASE_TAG
    #: Card capabilities the build has kernels for: [min, max).
    min_capability: tuple = (5, 0)
    max_capability: tuple = (12, 0)

    @property
    def url(self) -> str:
        return _RELEASE_URL.format(tag=self.tag, name=self.archive)


# Per-file digests of what is loaded or launched — whisper-cli.exe and every
# DLL — read off the pinned zips above. The manifest in the install folder is
# written by WinZapp but sits in a folder anything running as the user can
# rewrite, so verify_executable() holds the files to these instead.
_CPU_FILES = (
    ("Release/SDL2.dll",
     "de23db1694a3c7a4a735e7ecd3d214b2023cc2267922c6c35d30c7fc7370d677"),
    ("Release/ggml-base.dll",
     "cb1dfa532b8bf14c3cd54d8bd7ef12b8d07d3f7d85dbef01c135c1475e08ed38"),
    ("Release/ggml-cpu-alderlake.dll",
     "660886106a61537002c52cf0c7021bc8a8060174cc802ed650b9bfb77eb9183a"),
    ("Release/ggml-cpu-cannonlake.dll",
     "aab6d7e3c1707bd7cbb4da2061fd59fee5d6e3f4fe71606ab1a43ac814f1d89f"),
    ("Release/ggml-cpu-cascadelake.dll",
     "bef920f38f26432fa456ad7beda8a35b11d8719d10d198c08e61d1bc33c0ba40"),
    ("Release/ggml-cpu-haswell.dll",
     "f52a4824868b8d9ac48f814edeb4f7382e28371f093ef6e115077fb7125bf830"),
    ("Release/ggml-cpu-icelake.dll",
     "161baa9fb0061df74d0f0d83339a68890b2be1ba22b6b406d9a0ea23dbf89628"),
    ("Release/ggml-cpu-sandybridge.dll",
     "e5cb5b8ecbc52ffc05d54f8456db41eb17036bb6848592f7df8666f0ade12cdb"),
    ("Release/ggml-cpu-skylakex.dll",
     "4784b7f45e7b5199981e7ba8c391cd1b5d4aa74413a0929cfd0a6d909098c30e"),
    ("Release/ggml-cpu-sse42.dll",
     "674320166d86f18573f8e0e99efcdb90cfa6b7b05b42d9465976ef5e70e5e4a3"),
    ("Release/ggml-cpu-x64.dll",
     "ffc1938f2ce3b52cef0e0935c6ce953bb5cc5593757ea55872ae4c4ee8bd577a"),
    ("Release/ggml.dll",
     "4e77ead4ecc32324f9432acb06ee71444708880ca6780c918baa6903389ba257"),
    ("Release/llama.dll",
     "fdba0284d4cfbe366e7fbc8af764ac14328cda6fa34758e5521c03be68728640"),
    ("Release/parakeet.dll",
     "8f864b1008c8b98861583a09ea6035c547cb46a9715b609c7dd7ccca138d1b7e"),
    ("Release/whisper-cli.exe",
     "800a0fd754afa75e109c7248286ad735670fb6b23d92ca5d12604647ef638a65"),
    ("Release/whisper.dll",
     "0a29e5824c7495185b833ad07df7ab9cadf130a9be848f967c6b88aeca971566"),
)

_CUDA_FILES = (
    ("Release/SDL2.dll",
     "de23db1694a3c7a4a735e7ecd3d214b2023cc2267922c6c35d30c7fc7370d677"),
    ("Release/cublas64_12.dll",
     "e40202fe4223c1cd2d2dce7beec59e1ed61c7801bd827309183be9b50e358f4c"),
    ("Release/cublasLt64_12.dll",
     "2a896460bef60ed57ef32b0875812f355a6984e671d638bb632f5e8c1d7a831f"),
    ("Release/cudart64_12.dll",
     "d28e42265da7462162a54da6b7a99ea4fa2caf8139d862bb500db875d0b32dfc"),
    ("Release/ggml-base.dll",
     "de31a549b8d556590926eafc5b1d628a28eaa1a3aec50a06625c858df8f2363e"),
    ("Release/ggml-cpu-alderlake.dll",
     "7a5da87e1fe00809de5889bf7bbb5dc332142343d86c67fca5858324ce02ae8e"),
    ("Release/ggml-cpu-cannonlake.dll",
     "0aba518143556d1037c395d19fb40e34c7cdd1514a85fc8302388342cac22cce"),
    ("Release/ggml-cpu-cascadelake.dll",
     "0fd8759e4837b8287d0bb65b0c528e2682920679def12fbb007b5c9adde64c87"),
    ("Release/ggml-cpu-haswell.dll",
     "73a66c51cd7c3ca08a3d6812541b0104e97eabc9f1f3674542b55d5197361b49"),
    ("Release/ggml-cpu-icelake.dll",
     "b889deb2f9681e57e9432d848d80be14aabcf618b36063d86737f43f0a3b18d2"),
    ("Release/ggml-cpu-sandybridge.dll",
     "fcf516ae20ffe5de4e6f63696e68ff66790371f5d11a6864c92e3a7a76c52b8e"),
    ("Release/ggml-cpu-skylakex.dll",
     "e4d9282a55834cd3ffb482cffcb1871e5e0f4a817c7cf12ed2bc7440910f41f2"),
    ("Release/ggml-cpu-sse42.dll",
     "f1b46556613803d82438b1b62226e100017dc1f60cc8e6c5ec97420c67b23f9c"),
    ("Release/ggml-cpu-x64.dll",
     "2d682c0d8346b0de00c24c34a0d256db7609561a309466d2105f301b49061e95"),
    ("Release/ggml-cuda.dll",
     "21c03d8d41173774857da3119913b3a82148febe506c3a219d2bc0fe928cbd82"),
    ("Release/ggml.dll",
     "ce49bfa94df2769d31d6030e3862193b4985b6312e9315ae41a314f3b584ff2e"),
    ("Release/llama.dll",
     "a52fd15def683aef54d1bb727061b794ff4db78da32d483cd3bce922ee25303c"),
    ("Release/nvblas64_12.dll",
     "e42a77405e6e4b1cc661dcfcddead35ec62dcf59c6f9be1a3b5fab73d1f4c616"),
    ("Release/nvrtc-builtins64_124.dll",
     "79888dba26c51475ea21fc7b47d2b9dd5b1ffaecc8e5ea22a49fa5f5a722eb43"),
    ("Release/nvrtc64_120_0.dll",
     "3aa3cd8aa10437e212760c0e1ed730807811ec3bc330216dbfde4b26211d2243"),
    ("Release/parakeet.dll",
     "8ab0612e29c211dbeba10094763a767421dc4808cd019be6e44f8952c873c152"),
    ("Release/whisper-cli.exe",
     "41a586cac5863ebfc198cdc8ffb1642795543c1a4506c6be974e55adf301dccb"),
    ("Release/whisper.dll",
     "9e16e279afd90ab0d266a7bae89b2444cc4f485761de52a0853aa0a53ed97514"),
)


_X64_CPU = RuntimeBuild(
    id="cpu",
    archive="whisper-bin-x64.zip",
    archive_bytes=8_361_840,
    archive_sha256="c2a4b60edb11f7e11a9191ffb50929535527d4d91c9903dbe3e554583bbbc63d",
    uses_cuda=False,
    pinned_files=_CPU_FILES,
    server_sha256="9eb6ee297215f07ba77a6d588a6a2715f2235f665528529c377b775bbab3cd2d",
)
_X64_CUDA = RuntimeBuild(
    id="cuda-12.4",
    archive="whisper-cublas-12.4.0-bin-x64.zip",
    archive_bytes=671_045_732,
    archive_sha256="c1b17166e1e31a91cc8e9c1f910d3785e3ce757bb2958bf9dce13fdb4880005f",
    uses_cuda=True,
    pinned_files=_CUDA_FILES,
    # Read off the pinned zip (its own sha256 and size checked first).
    server_sha256="0b54514e66f5d2a445098857e346389816b58f9872f402fde76cf1c8163853af",
)

# Windows on ARM64 (machine_arch): release b5454, the first with ARM64 builds,
# measured off its zips (GitHub's own digest of each zip agreed). A native
# ARM64 whisper-cli instead of the x64 one under emulation, and the only
# NVIDIA build there: CUDA 13.4, offered like the x64 CUDA build, as an option.
# CUDA 13 dropped every card older than Turing (sm_75) and has Blackwell.
ARM64_RELEASE_TAG = "b5454"
_ARM64_CPU_FILES = (
    ("Release/ggml-base.dll", "7c8698821411149cfdebf587a26263bcb43e7ab396759e24eb641f665b10b45e"),
    ("Release/ggml-cpu.dll", "139a72c067da8862aea7d19246668884852c444af343761c1535dc32bed2ad81"),
    ("Release/ggml.dll", "bd4a483fd315a95a66f43283c710f77d42cc13315829cb0a3ecffc30b843ac9f"),
    ("Release/libomp140.aarch64.dll",
     "d2649698fc68466ee88cf58ae3ab0fab106f29326e83aaaa5643d30651a962e3"),
    ("Release/parakeet.dll", "6fd14777559fa4f068ed1a3735c1dbe0f065fe9316bd7172667cb4809d1af669"),
    ("Release/whisper-cli.exe", "59dfbb9c0ecdb9a295b5646188005a2840e5c42a5fca134ff15bc95eba027e7c"),
    ("Release/whisper.dll", "48214430bbfce6cd656125c06d1c368286412681dab0242061c5cafa57913419"),
)
_ARM64_CUDA_FILES = (
    ("Release/cublas64_13.dll", "49e8fa23d88ac0cbae27e200fc092b894dd55b4de5cb4293993986e29fb9b65a"),
    ("Release/cublasLt64_13.dll", "daf579ae36bb3c85e2340c57a1994f634e07c6f55ef70dd4cdc26695de21c752"),
    ("Release/cudart64_13.dll", "bd927ddf03823eeead7c8b261a8669d96764b89109311f4ab6accde8b7d97ec2"),
    ("Release/ggml-base.dll", "51d6774aa9d9046ffa76a3f777201ed3f085fa0176ec740f52a18c450a31e9f0"),
    ("Release/ggml-cpu.dll", "79d28618e66188d519f94f8b7dde10610a7235c95aca8a4901765314cd31cc76"),
    ("Release/ggml-cuda.dll", "16e9e3270b306a3bd8ab59d3e0cf4d2ef2d172c72d406d4ed824dfabd9202d28"),
    ("Release/ggml.dll", "31ee7f066f12f5cef5b523f3bcb934a7af016a1d398d03ac4979d8d85071a33c"),
    ("Release/libomp140.aarch64.dll",
     "d2649698fc68466ee88cf58ae3ab0fab106f29326e83aaaa5643d30651a962e3"),
    ("Release/parakeet.dll", "4471245a53c5b71dadc9da9768020d5fd3da6f22806da09b58e8d0a0ae9df19f"),
    ("Release/whisper-cli.exe", "aa1e654a61031dcd8ffaafc334c7163017d24acf6ce5ab100bc561e789799e84"),
    ("Release/whisper.dll", "7c957081d3dc74e8c291ea98d48b56621448f0f719e0f0e7550ef4cae297cfea"),
)
_ARM64_CPU = RuntimeBuild(
    id="cpu-arm64",
    archive="whisper-bin-win-cpu-arm64.zip",
    archive_bytes=4_371_193,
    archive_sha256="28c37e7b598c3d9bbfef94f3bd67f2ee6f12b7c86f3da5edcf5e4308d615ff6d",
    uses_cuda=False,
    pinned_files=_ARM64_CPU_FILES,
    server_sha256="c5b3d51a2f327f0e8d793f02cbbb05f656b11b9e365976e84e88965d9de72f7c",
    tag=ARM64_RELEASE_TAG,
)
_ARM64_CUDA = RuntimeBuild(
    id="cuda-13.4-arm64",
    archive="whisper-bin-win-cuda-13.4-arm64.zip",
    archive_bytes=291_157_544,
    archive_sha256="6184ae8887d38ea9add68e883b9c846e7bf562ba1bf619ffc7ee9f8ec7e1699c",
    uses_cuda=True,
    pinned_files=_ARM64_CUDA_FILES,
    server_sha256="159da225d2faab84db0be93b5c83ac2c4c12be818ccbc32d4770d68449e8c3aa",
    tag=ARM64_RELEASE_TAG,
    min_capability=(7, 5),
    max_capability=(13, 0),
)


def builds_for(arm64: bool) -> tuple:
    """(processor build, CUDA build) for this machine's architecture."""
    return (_ARM64_CPU, _ARM64_CUDA) if arm64 else (_X64_CPU, _X64_CUDA)


def _machine_is_arm64() -> bool:
    from core.transcription import machine_arch
    return machine_arch.native_is_arm64()


#: Chosen once, for the machine WinZapp runs on.
BUILD_CPU, BUILD_CUDA = builds_for(_machine_is_arm64())
BUILDS = (BUILD_CPU, BUILD_CUDA)


def cuda_build_supported(compute_capability, build=None) -> bool:
    """Whether the CUDA build may be offered to a card of this capability.

    None (NVML could not describe the card, or there is none) answers False.
    """
    if compute_capability is None:
        return False
    build = build or BUILD_CUDA
    capability = tuple(compute_capability)
    return tuple(build.min_capability) <= capability < tuple(build.max_capability)


def builds_offered(compute_capability) -> tuple[RuntimeBuild, ...]:
    """The builds worth offering here: the CPU one, plus CUDA when it can run."""
    if cuda_build_supported(compute_capability):
        return (BUILD_CPU, BUILD_CUDA)
    return (BUILD_CPU,)


def safe_member_path(name):
    """A zip member's name as a relative path ("a/b.dll"), or None to refuse it.

    Refused: an empty name, an absolute one ("/x", "\\\\x"), any ".." segment,
    and any ":" — which covers a drive ("C:x") and an NTFS stream
    ("x.dll:evil") alike. Also refused, because Windows does not create what
    they say: a segment ending in "." or " " (silently trimmed, so "a.dll." and
    "a.dll" collide) and a DOS device name ("NUL", "com1.dll"). Both
    separators are read, since the zip is written elsewhere and extracted on
    Windows, where either one is a separator.
    """
    normalized = str(name or "").replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return None
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(_unsafe_segment(part) for part in parts):
        return None
    return "/".join(parts)


def _unsafe_segment(part) -> bool:
    return (
        part == ".."
        or ":" in part
        or part.endswith((".", " "))
        or _DOS_DEVICE.match(part) is not None
    )


def locate_executable(relative_paths):
    """The shallowest `whisper-cli.exe` among `relative_paths`, or None.

    Matched on the file name alone and without regard to case, because the
    folder it sits in is exactly what was not verified.
    """
    candidates = [
        path for path in relative_paths
        if path.rsplit("/", 1)[-1].lower() == EXECUTABLE_NAME
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda path: (path.count("/"), path))
