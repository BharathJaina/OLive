# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import argparse
import glob
import os
import shutil
import subprocess
import sys


def is_windows():
    return sys.platform.startswith("win")


def copy(src_path, dest_path):
    files = glob.glob(src_path)
    for file in files:
        shutil.copy(file, dest_path, follow_symlinks=False)


def build_onnxruntime(onnxruntime_dir, config, build_args, build_name, args):
    if args.variants and not (build_name in args.variants.split(",")):
        return

    if is_windows():
        windows_build_dir = os.path.join(onnxruntime_dir, "build", "Windows", config, config)
        perf_test_exe = os.path.join(windows_build_dir, "onnxruntime_perf_test.exe")
        if not os.path.exists(perf_test_exe) and args.prebuilt:
            print("Not prebuilt onnxruntime found. Building onnxruntime.")
            args.prebuilt = False
        if not args.prebuilt:
            # Remove cache for a clean build
            if os.path.exists(os.path.join(windows_build_dir, "CMakeCache.txt")):
                os.remove(os.path.join(windows_build_dir, "CMakeCache.txt"))
            subprocess.run(
                [os.path.join(onnxruntime_dir, "build.bat"), "--config", config, "--build_shared_lib", "--parallel"] +
                build_args,
                cwd=onnxruntime_dir,
                check=True)
            target_dir = os.path.join("bin", config, build_name)
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        os.makedirs(target_dir)

        copy(os.path.join(windows_build_dir, "onnxruntime_perf_test.exe"), target_dir)
        copy(os.path.join(windows_build_dir, "onnxruntime.dll"), target_dir)
        if "all_eps" in build_name:
            copy(os.path.join(windows_build_dir, "dnnl.dll"), target_dir)
            if args.use_cuda or args.use_tensorrt:
                copy(os.path.join(args.cudnn_home, "bin/cudnn*.dll"), target_dir)
            if args.use_tensorrt:
                copy(os.path.join(args.tensorrt_home, "lib/nvinfer.dll"), target_dir)
        if "mklml" in build_name:
            copy(os.path.join(windows_build_dir, "tvm.dll"), target_dir)
            if args.use_nuphar:
                copy(
                    os.path.join(onnxruntime_dir, "onnxruntime", "core", "providers", "nuphar", "scripts",
                                    "symbolic_shape_infer.py"), target_dir)
    else:
        linux_build_dir = os.path.join(onnxruntime_dir, "build", "Linux", config)
        perf_test_exe = os.path.join(linux_build_dir, "onnxruntime_perf_test")
        if not os.path.exists(perf_test_exe) and args.prebuilt:
            print("Not prebuilt onnxruntime found. Building onnxruntime.")
            args.prebuilt = False
        if not args.prebuilt:
            # Remove cache for a clean build
            if os.path.exists(os.path.join(linux_build_dir, "CMakeCache.txt")):
                os.remove(os.path.join(linux_build_dir, "CMakeCache.txt"))
            build_env = os.environ.copy()
            lib_path = os.path.join(linux_build_dir, "mklml", "src", "project_mklml", "lib")
            if "LD_LIBRARY_PATH" in build_env:
                build_env["LD_LIBRARY_PATH"] += os.pathsep + lib_path
            else:
                build_env["LD_LIBRARY_PATH"] = lib_path

            if args.use_tensorrt:
                build_env["LD_LIBRARY_PATH"] += os.pathsep + args.tensorrt_home
            subprocess.run(
                [os.path.join(onnxruntime_dir, "build.sh"), "--config", config, "--build_shared_lib", "--parallel"] +
                build_args,
                cwd=onnxruntime_dir,
                check=True,
                env=build_env)

        target_dir = os.path.join("bin", config, build_name)

        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        os.makedirs(target_dir)

        copy(os.path.join(linux_build_dir, "onnxruntime_perf_test"), target_dir)
        copy(os.path.join(linux_build_dir, "libonnxruntime.so*"), target_dir)
        
        if "all_eps" in build_name:
            copy(os.path.join(linux_build_dir, "dnnl/install/lib/libdnnl.so*"), target_dir)
            copy(os.path.join(linux_build_dir, "libonnxruntime_providers_dnnl.so*"), target_dir)
            if args.use_cuda or args.use_tensorrt:
                copy(os.path.join(args.cudnn_home, "lib64/libcudnn.so*"), target_dir)
                copy(os.path.join(args.cudnn_home, "lib64/libnvrtc.so*"), target_dir)
            if args.use_tensorrt:
                copy(os.path.join(args.tensorrt_home, "lib/libnvinfer.so*"), target_dir)
                copy(os.path.join(args.tensorrt_home, "lib/libnvinfer_plugin.so*"), target_dir)
                copy(os.path.join(args.tensorrt_home, "lib/libmyelin.so*"), target_dir)
        if "mklml" in build_name:
            copy(os.path.join(linux_build_dir, "mklml/src/project_mklml/lib/*.so*"), target_dir)
            if args.use_nuphar:
                copy(os.path.join(linux_build_dir, "external", "tvm", "libtvm.so*"), target_dir)
                copy(
                    os.path.join(onnxruntime_dir, "onnxruntime", "core", "providers", "nuphar", "scripts",
                                    "symbolic_shape_infer.py"), target_dir)
        if "ngraph" in build_name:
            copy(os.path.join(linux_build_dir, "external/ngraph/lib/lib*.so*"), target_dir)
            copy(os.path.join(linux_build_dir, "external", "tvm", "libtvm.so*"), target_dir)


def parse_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument("--onnxruntime_home", required=True, help="Path to onnxruntime home.")

    parser.add_argument("--config",
                        default="RelWithDebInfo",
                        choices=["Debug", "MinSizeRel", "Release", "RelWithDebInfo"],
                        help="Configuration to build.")
    parser.add_argument("--use_cuda", action='store_true', help="Enable CUDA.")
    parser.add_argument("--cuda_version",
                        help="The version of CUDA toolkit to use. Auto-detect if not specified. e.g. 9.0")
    parser.add_argument(
        "--cuda_home",
        help="Path to CUDA home."
        "Read from CUDA_HOME environment variable if --use_cuda is true and --cuda_home is not specified.")
    parser.add_argument(
        "--cudnn_home",
        help="Path to CUDNN home. "
        "Read from CUDNN_HOME environment variable if --use_cuda is true and --cudnn_home is not specified.")
    parser.add_argument("--use_tensorrt", action='store_true', help="Build with TensorRT")
    parser.add_argument("--tensorrt_home", help="Path to TensorRT installation dir")

    parser.add_argument("--use_ngraph", action='store_true', help="Build with nGraph")
    parser.add_argument("--use_mklml", action='store_true', help="Build with mklml")
    parser.add_argument("--use_nuphar", action='store_true', help="Build with Nuphar")
    parser.add_argument("--llvm_path", help="Path to llvm-build/lib/cmake/llvm")

    parser.add_argument("--variants", help="Variants to build. Will build all by default")
    parser.add_argument("--prebuilt",
                        action='store_true',
                        help="Set to true if a prebuilt onnxruntime is available for the specified execution provider."
                        "Default is False, which will build onnxruntime with all specified execution provider.")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    if args.prebuilt:
        build_name = "all_eps"
        if args.use_nuphar or args.use_mklml:
            build_name = "mklml"
        if args.use_ngraph:
            build_name = "ngraph"
        build_onnxruntime(args.onnxruntime_home, args.config, [], build_name, args)
    else:
        build_args = []

        if args.use_cuda:
            build_args += ["--use_cuda"]
            if args.cuda_version:
                build_args = build_args + ["--cuda_version", args.cuda_version]
            if args.cuda_home:
                build_args = build_args + ["--cuda_home", args.cuda_home]
            if args.cudnn_home:
                build_args = build_args + ["--cudnn_home", args.cudnn_home]

        if args.use_tensorrt:
            build_args += ["--use_tensorrt", "--use_full_protobuf"]
            if args.tensorrt_home:
                build_args = build_args + ["--tensorrt_home", args.tensorrt_home]
            if not args.use_cuda:
                if args.cuda_version:
                    build_args = build_args + ["--cuda_version", args.cuda_version]
                if args.cuda_home:
                    build_args = build_args + ["--cuda_home", args.cuda_home]
                if args.cudnn_home:
                    build_args = build_args + ["--cudnn_home", args.cudnn_home]

        # Build CPU with no OpenMp as a separate build
        build_onnxruntime(args.onnxruntime_home, args.config, build_args, "mlas_eps", args)

        nuphar_args = ["--use_tvm", "--use_llvm", "--use_nuphar"] if args.use_nuphar else []
        nuphar_args = nuphar_args + ["--llvm_path", args.llvm_path] if args.llvm_path else nuphar_args

        if args.use_mklml:
            # Build mklml as a separate build
            build_onnxruntime(args.onnxruntime_home, args.config, ["--use_mklml"] + nuphar_args, "mklml", args)
        elif args.use_nuphar:
            raise ValueError("Please build with --use_mklml to use nuphar. ")

        if args.use_ngraph:
            # Build ngraph as a separate build
            build_onnxruntime(args.onnxruntime_home, args.config, ["--use_openmp", "--use_ngraph"], "ngraph",
                            args)

        build_args = ["--use_dnnl", "--use_openmp"]       

        # Build cpu_openmp, cuda, dnnl, nuphar, ngraph and tensorrt in one build.
        build_onnxruntime(args.onnxruntime_home, args.config, build_args, "omp_eps", args)