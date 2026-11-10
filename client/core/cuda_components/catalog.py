"""The pinned CUDA components (generated from PyPI on 2026-11-09; see
cuda_components/__init__.py). Each wheel is pinned whole: URL, size and
sha256 describe one artifact; the files kept out of it are checked against
the wheel's own RECORD at install time."""

from core.cuda_components.model import Component

COMPONENTS = {
    "ort_gpu_cu12": Component(
        id="ort_gpu_cu12", title="ONNX Runtime GPU 1.26 (CUDA 12)",
        url="https://files.pythonhosted.org/packages/67/3f/59f1777a394625ecc9a85636de57dc47c25dbb5f888da050f1463955a0ce/onnxruntime_gpu-1.26.0-cp313-cp313-win_amd64.whl",
        filename="onnxruntime_gpu-1.26.0-cp313-cp313-win_amd64.whl", wheel_bytes=226548083,
        sha256="6ab9f9c741d2e239b2e321ab0d389c04329d4ab7f11e3b92dd3aa7db1c59dee4",
        dist_prefix="onnxruntime_gpu-1.26.0.dist-info/", extracted_bytes=379543494,
        members=(), tree="onnxruntime/"),
    "cudart_cu12": Component(
        id="cudart_cu12", title="CUDA Runtime 12",
        url="https://files.pythonhosted.org/packages/59/df/e7c3a360be4f7b93cee39271b792669baeb3846c58a4df6dfcf187a7ffab/nvidia_cuda_runtime_cu12-12.9.79-py3-none-win_amd64.whl",
        filename="nvidia_cuda_runtime_cu12-12.9.79-py3-none-win_amd64.whl", wheel_bytes=3591604,
        sha256="8e018af8fa02363876860388bd10ccb89eb9ab8fb0aa749aaf58430a9f7c4891",
        dist_prefix="nvidia_cuda_runtime_cu12-12.9.79.dist-info/", extracted_bytes=583680,
        members=("nvidia/cuda_runtime/bin/cudart64_12.dll",), tree=""),
    "cufft_cu12": Component(
        id="cufft_cu12", title="cuFFT 11 (CUDA 12)",
        url="https://files.pythonhosted.org/packages/20/ee/29955203338515b940bd4f60ffdbc073428f25ef9bfbce44c9a066aedc5c/nvidia_cufft_cu12-11.4.1.4-py3-none-win_amd64.whl",
        filename="nvidia_cufft_cu12-11.4.1.4-py3-none-win_amd64.whl", wheel_bytes=200067309,
        sha256="8e5bfaac795e93f80611f807d42844e8e27e340e0cde270dcb6c65386d795b80",
        dist_prefix="nvidia_cufft_cu12-11.4.1.4.dist-info/", extracted_bytes=287136768,
        members=("nvidia/cufft/bin/cufft64_11.dll",), tree=""),
    "curand_cu12": Component(
        id="curand_cu12", title="cuRAND (CUDA 12)",
        url="https://files.pythonhosted.org/packages/e5/98/1bd66fd09cbe1a5920cb36ba87029d511db7cca93979e635fd431ad3b6c0/nvidia_curand_cu12-10.3.10.19-py3-none-win_amd64.whl",
        filename="nvidia_curand_cu12-10.3.10.19-py3-none-win_amd64.whl", wheel_bytes=68774847,
        sha256="e8129e6ac40dc123bd948e33d3e11b4aa617d87a583fa2f21b3210e90c743cde",
        dist_prefix="nvidia_curand_cu12-10.3.10.19.dist-info/", extracted_bytes=79197696,
        members=("nvidia/curand/bin/curand64_10.dll",), tree=""),
    "nvjitlink_cu12": Component(
        id="nvjitlink_cu12", title="nvJitLink 12",
        url="https://files.pythonhosted.org/packages/dd/7e/2eecb277d8a98184d881fb98a738363fd4f14577a4d2d7f8264266e82623/nvidia_nvjitlink_cu12-12.9.86-py3-none-win_amd64.whl",
        filename="nvidia_nvjitlink_cu12-12.9.86-py3-none-win_amd64.whl", wheel_bytes=35584936,
        sha256="cc6fcec260ca843c10e34c936921a1c426b351753587fdd638e8cff7b16bb9db",
        dist_prefix="nvidia_nvjitlink_cu12-12.9.86.dist-info/", extracted_bytes=87359488,
        members=("nvidia/nvjitlink/bin/nvJitLink_120_0.dll",), tree=""),
    "nvrtc_cu12": Component(
        id="nvrtc_cu12", title="NVRTC 12",
        url="https://files.pythonhosted.org/packages/52/de/823919be3b9d0ccbf1f784035423c5f18f4267fb0123558d58b813c6ec86/nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl",
        filename="nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl", wheel_bytes=76408187,
        sha256="72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349",
        dist_prefix="nvidia_cuda_nvrtc_cu12-12.9.86.dist-info/", extracted_bytes=97050624,
        members=("nvidia/cuda_nvrtc/bin/nvrtc-builtins64_129.dll", "nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll",), tree=""),
    "cudnn_cu12": Component(
        id="cudnn_cu12", title="cuDNN 9 (CUDA 12)",
        url="https://files.pythonhosted.org/packages/c7/bd/1d6709d2223e99f04079f16005f8ae6e11a14ee16b5eab6833686cf72fe1/nvidia_cudnn_cu12-9.28.1.4-py3-none-win_amd64.whl",
        filename="nvidia_cudnn_cu12-9.28.1.4-py3-none-win_amd64.whl", wheel_bytes=696300805,
        sha256="3467aafa9b260e6adf5fbbfc0e21d180133305b42032aa1160be2d3142bb8b09",
        dist_prefix="nvidia_cudnn_cu12-9.28.1.4.dist-info/", extracted_bytes=1084201568,
        members=("nvidia/cudnn/bin/cudnn64_9.dll", "nvidia/cudnn/bin/cudnn_adv64_9.dll", "nvidia/cudnn/bin/cudnn_cnn64_9.dll", "nvidia/cudnn/bin/cudnn_engines_precompiled64_9.dll", "nvidia/cudnn/bin/cudnn_engines_runtime_compiled64_9.dll", "nvidia/cudnn/bin/cudnn_engines_tensor_ir64_9.dll", "nvidia/cudnn/bin/cudnn_ext64_9.dll", "nvidia/cudnn/bin/cudnn_graph64_9.dll", "nvidia/cudnn/bin/cudnn_heuristic64_9.dll", "nvidia/cudnn/bin/cudnn_ops64_9.dll",), tree=""),
    "ort_gpu_cu13": Component(
        id="ort_gpu_cu13", title="ONNX Runtime GPU 1.29 (CUDA 13)",
        url="https://files.pythonhosted.org/packages/ab/ab/975b1f0d15d4fd4622ada6bf50de81512d2bda18cca953e4c095061eb7b7/onnxruntime_gpu-1.29.0-cp313-cp313-win_amd64.whl",
        filename="onnxruntime_gpu-1.29.0-cp313-cp313-win_amd64.whl", wheel_bytes=148181781,
        sha256="c37187aaa39802283f47094ccdfa5a0667b7f9723feed67facf2cef6e0e6a6dc",
        dist_prefix="onnxruntime_gpu-1.29.0.dist-info/", extracted_bytes=213671338,
        members=(), tree="onnxruntime/"),
    "cudart_cu13": Component(
        id="cudart_cu13", title="CUDA Runtime 13",
        url="https://files.pythonhosted.org/packages/35/99/e07dd9565d2e0317c1a62ff39ae9fc0542c048f74bcfc777c8be97710347/nvidia_cuda_runtime-13.5.37-py3-none-win_amd64.whl",
        filename="nvidia_cuda_runtime-13.5.37-py3-none-win_amd64.whl", wheel_bytes=2780254,
        sha256="f225d3fa5ea6bfbb58fc4979ef72375cbe661d9107f0d8a4ff22cc151525e4a9",
        dist_prefix="nvidia_cuda_runtime-13.5.37.dist-info/", extracted_bytes=551024,
        members=("nvidia/cu13/bin/x86_64/cudart64_13.dll",), tree=""),
    "cufft_cu13": Component(
        id="cufft_cu13", title="cuFFT 12 (CUDA 13)",
        url="https://files.pythonhosted.org/packages/94/79/8f2a7125f560b86571c0be3dbf10e33041187bc4a8cf66107541fe8e5cce/nvidia_cufft-12.5.0.14-py3-none-win_amd64.whl",
        filename="nvidia_cufft-12.5.0.14-py3-none-win_amd64.whl", wheel_bytes=164476208,
        sha256="78e7585159ecf37d73af1ce43a5dea7fd3360e1af71cde08f070c07966a336bc",
        dist_prefix="nvidia_cufft-12.5.0.14.dist-info/", extracted_bytes=236194928,
        members=("nvidia/cu13/bin/x86_64/cufft64_12.dll",), tree=""),
    "curand_cu13": Component(
        id="curand_cu13", title="cuRAND (CUDA 13)",
        url="https://files.pythonhosted.org/packages/ee/b6/5b38490991b6875ae60c4f41e190ac91cadd1e6a1324b152999cc4a3f580/nvidia_curand-10.4.5.25-py3-none-win_amd64.whl",
        filename="nvidia_curand-10.4.5.25-py3-none-win_amd64.whl", wheel_bytes=77330709,
        sha256="f6a02a70f5b9715235df31a164a4bf3237a45be1600a5f435f5e4a0d8682eb22",
        dist_prefix="nvidia_curand-10.4.5.25.dist-info/", extracted_bytes=105042032,
        members=("nvidia/cu13/bin/x86_64/curand64_10.dll",), tree=""),
    "nvjitlink_cu13": Component(
        id="nvjitlink_cu13", title="nvJitLink 13",
        url="https://files.pythonhosted.org/packages/a7/53/c43398492e8374880d5e01c23c4dac33ba0c3bd03515d6ecc574254c0967/nvidia_nvjitlink-13.5.37-py3-none-win_amd64.whl",
        filename="nvidia_nvjitlink-13.5.37-py3-none-win_amd64.whl", wheel_bytes=41467665,
        sha256="4465d4e8f2212118220b580c82523c5654f93a62906bd51d29d79bdfc12376c1",
        dist_prefix="nvidia_nvjitlink-13.5.37.dist-info/", extracted_bytes=103557232,
        members=("nvidia/cu13/bin/x86_64/nvJitLink_130_0.dll",), tree=""),
    "nvrtc_cu13": Component(
        id="nvrtc_cu13", title="NVRTC 13",
        url="https://files.pythonhosted.org/packages/9f/6c/241c5155199a6de55bad8862213ed64b487dc9526aae817e4af31afea1a4/nvidia_cuda_nvrtc-13.5.37-py3-none-win_amd64.whl",
        filename="nvidia_cuda_nvrtc-13.5.37-py3-none-win_amd64.whl", wheel_bytes=49248991,
        sha256="e8e6c10b251385707ae3abd7b4ee118de6736fd7678d0f0f4eab4d759f679c21",
        dist_prefix="nvidia_cuda_nvrtc-13.5.37.dist-info/", extracted_bytes=119174368,
        members=("nvidia/cu13/bin/x86_64/nvrtc-builtins64_135.dll", "nvidia/cu13/bin/x86_64/nvrtc64_130_0.dll",), tree=""),
    "cublas_cu13": Component(
        id="cublas_cu13", title="cuBLAS 13",
        url="https://files.pythonhosted.org/packages/12/39/e1b1b4adb8b171894d9a89035df0596d448cf73dc403802d24dbe30cc0be/nvidia_cublas-13.10.0.20-py3-none-win_amd64.whl",
        filename="nvidia_cublas-13.10.0.20-py3-none-win_amd64.whl", wheel_bytes=427284863,
        sha256="c310b749eea855b88fafb906de08d6819b87216c6711640771764fea5dcd8546",
        dist_prefix="nvidia_cublas-13.10.0.20.dist-info/", extracted_bytes=554315488,
        members=("nvidia/cu13/bin/x86_64/cublas64_13.dll", "nvidia/cu13/bin/x86_64/cublasLt64_13.dll",), tree=""),
    "cudnn_cu13": Component(
        id="cudnn_cu13", title="cuDNN 9 (CUDA 13)",
        url="https://files.pythonhosted.org/packages/48/9c/26e6bdd31c97c389aa565b49c916be0dec17dd61dad6cd33fee286d1b946/nvidia_cudnn_cu13-9.28.1.4-py3-none-win_amd64.whl",
        filename="nvidia_cudnn_cu13-9.28.1.4-py3-none-win_amd64.whl", wheel_bytes=407065099,
        sha256="73d874c6bd8a1c75ca040228f77c4e163efe9bab3b65bd85914e2b7334ac68ee",
        dist_prefix="nvidia_cudnn_cu13-9.28.1.4.dist-info/", extracted_bytes=602833504,
        members=("nvidia/cudnn/bin/cudnn64_9.dll", "nvidia/cudnn/bin/cudnn_adv64_9.dll", "nvidia/cudnn/bin/cudnn_cnn64_9.dll", "nvidia/cudnn/bin/cudnn_engines_precompiled64_9.dll", "nvidia/cudnn/bin/cudnn_engines_runtime_compiled64_9.dll", "nvidia/cudnn/bin/cudnn_engines_tensor_ir64_9.dll", "nvidia/cudnn/bin/cudnn_ext64_9.dll", "nvidia/cudnn/bin/cudnn_graph64_9.dll", "nvidia/cudnn/bin/cudnn_heuristic64_9.dll", "nvidia/cudnn/bin/cudnn_ops64_9.dll",), tree=""),
}
