# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import argparse
import json
import glob
import os
import shutil
import subprocess
import sys
import uuid
import collections
import psutil
import GPUtil
from monitor import Monitor
from maps import ep_envvar_map, ep_graphOptimizer_map, model_ep_map

def is_windows():
    return sys.platform.startswith("win")

def remove(file):
    if os.path.exists(file):
        os.remove(file)

class PerfTestParams:
    def __init__(self, name, desc, path, test_args, env, args, build_name):
        
        self.name = name
        self.desc = desc
        self.path = path
        
        self.env = os.environ.copy()
        if is_windows():
            self.exe = os.path.join(build_path, "onnxruntime_perf_test.exe")
        else:
            self.exe = os.path.join(build_path, "onnxruntime_perf_test")
            self.env["LD_LIBRARY_PATH"] = build_path
        
        self.test_args = [self.exe] + test_args
        self.env.pop("OMP_WAIT_POLICY", None)
        self.env.pop("OMP_NUM_THREADS", None)
        self.env.update(env)
        self.env_to_set = env
        
        self.model = args.model
        self.result_dir = args.result

        self.args = args
        self.build_name = build_name

        self.command = None
        self.avg = None

        self.gpu = 0
        self.cpu = 0
        self.memory = 0

    def updateEnv(self, env):
        self.env.update(env)
        self.env_to_set.update(env)

    def get_common_args(self):
        common_args = ["-o", self.args.optimization_level]
        if self.args.test_mode:
            common_args = common_args + ["-m", self.args.test_mode]
        if self.args.test_mode == "times":
            if self.args.repeated_times:
                common_args = common_args + ["-r", self.args.repeated_times]
        else:
            if self.args.duration_time:
                common_args = common_args + ["-t", self.args.duration_time]
        return common_args

    def get_args(self, result_file):
        return self.test_args + self.get_common_args() + [self.model, result_file]

    def get_percentiles_args(self, result_file):
        return self.test_args + ["-o", self.args.optimization_level, "-m", "times", "-r", "200", self.model, result_file]

    def get_profile_args(self, profile_file, result_file):
        return self.test_args + self.get_common_args() + ["-p", profile_file, self.model, result_file]

    def get_graph_optimization_level(self):
        if self.args.optimization_level == "1":
            return "ORT_ENABLE_BASIC"
        if self.args.optimization_level == "2":
            return "ORT_ENABLE_EXTENDED"
        if self.args.optimization_level == "99":
            return "ORT_ENABLE_ALL"
    
    def get_inter_threads_code_snippet(self):
        try:
            index = self.test_args.index("-y")        
        except ValueError:
            return ""
        if index + 1 >= len(self.test_args):
            return ""
        return "so.inter_op_num_threads = " + self.test_args[index + 1]

    def get_intra_threads_code_snippet(self):
        try:
            index = self.test_args.index("-x")
        except ValueError:
            return ""
        if index + 1 >= len(self.test_args):
            return ""
        return "so.intra_op_num_threads = " + self.test_args[index + 1]
    
    def get_execution_provider_name(self):
        try:
            index = self.test_args.index("-e")
        except ValueError:
            return "CPUExecutionProvider"
        if index + 1 >= len(self.test_args):
            return ""
        return self.test_args[index + 1].upper() + "ExecutionProvider"

    def print_args(self, args):
        if self.env.get("OMP_WAIT_POLICY"):
            print("OMP_WAIT_POLICY=" + self.env["OMP_WAIT_POLICY"])
        if self.env.get("OMP_NUM_THREADS"):
            print("OMP_NUM_THREADS=" + self.env["OMP_NUM_THREADS"])
        print(" ".join(args))

    def gen_code_snippet(self):
        code_snippet = {
            "execution_provider": self.build_name,
            "environment_variables": self.env_to_set,
            "code": "\
                import onnxruntime as ort \
                so = rt.SessionOptions() \
                so.graph_optimization_level = ort.GraphOptimizationLevel.{}\
                so.execution_mode = rt.ExecutionMode.{} \
                {} \
                {} \
                session = rt.Session(\"{}\", so, providers=[\"{}\"]) \
                ".format(self.get_graph_optimization_level(), 
                "ORT_PARALLEL" if "-P" in self.test_args else "ORT_SEQUENTIAL", 
                self.get_intra_threads_code_snippet(), 
                self.get_inter_threads_code_snippet(), 
                self.args.model, 
                self.get_execution_provider_name())
        }
        return code_snippet


def run_perf_tuning(test_params, percentiles=False):
    print()

    result_file = os.path.join(test_params.result_dir, str(uuid.uuid4()))

    if percentiles:
        test_args = test_params.get_percentiles_args(result_file)
    else:
        test_args = test_params.get_args(result_file)
    perf_tuning = subprocess.run(test_args, env=test_params.env, capture_output=True)
    # The first run was warmup.
    remove(result_file)
    test_params.print_args(test_args)
    if not test_params.command:
        test_params.command = " ".join(test_args)
    perf_tuning = subprocess.run(test_args, env=test_params.env)
    latencies = []
    if perf_tuning.returncode == 0 and os.path.exists(result_file):
        with open(result_file) as result:
            for line in result.readlines():
                line = line.strip()
                tokens = line.split(",")
                if len(tokens) == 5:
                    latencies.append(float(tokens[1]))

        if percentiles:
            # Profile in case of success
            profile_name = os.path.join(test_params.result_dir, str(uuid.uuid4()))
            m = Monitor(latencies[0])
            perf_tuning = subprocess.run(test_params.get_profile_args(profile_name, result_file), 
                env=test_params.env,
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            m.stop()
            m.recorded_gpu = [x for x in m.recorded_gpu if not x == 0]
            m.recorded_cpu = [x for x in m.recorded_cpu if not x == 0]            
            test_params.gpu = round(sum(m.recorded_gpu)/len(m.recorded_gpu), 5) if len(m.recorded_gpu) > 0 else 0
            test_params.cpu = round(sum(m.recorded_cpu) / len(m.recorded_cpu), 5) if len(m.recorded_cpu) > 0 else 0
            test_params.memory = round(sum(m.recorded_memory) / len(m.recorded_memory), 5) if len(m.recorded_memory) > 0 else 0
            if perf_tuning.returncode == 0:
                # Find profile result
                files = glob.glob(profile_name + "*")
                if len(files) == 1:
                    with open(files[0]) as profile:
                        with open(os.path.join(test_params.result_dir, "profile_" + test_params.name + ".tsv"), "w") as out_profile:
                            print("cat\tpid\ttid\tdur\tts\tph\tname\targs", file=out_profile)
                            for record in json.load(profile):
                                print(str(record["cat"]) + "\t" + str(record["pid"]) + "\t" + str(record["tid"]) + "\t" + str(record["dur"]) + "\t" + str(record["ts"]) + "\t" + str(record["ph"]) + "\t" + str(record["name"]) + "\t" + str(record["args"]), file=out_profile)
                    shutil.move(files[0], os.path.join(test_params.result_dir, "profile_" + test_params.name + ".json"))

    remove(result_file)

    latencies.sort()

    test_params.latencies = latencies

    if len(latencies) > 0:
        test_params.avg = round(sum(latencies) / len(latencies), 9) * 1000
    else:
        test_params.avg = None

    print(test_params.name, test_params.avg)

def run_perf_tuning_binary(test_params, num_cores, name_suffix, desc_suffix, failed_tests, successful_tests, is_omp=False, tune_inter_ops=False):
    lower = 2
    upper = num_cores if is_omp else num_cores - 1
    mid = lower + (upper - lower) // 2
    best_run = -1
    if lower > upper:
        return best_run
    best_latency = float("inf")

    # Run with lowest number of cores to get a baseline of latency
    param = PerfTestParams(
        test_params.name + str(lower) + name_suffix,
        test_params.desc + str(lower) + desc_suffix,
        test_params.path,
        [],
        test_params.env_to_set.copy(),
        test_params.args,
        build_name,
    )
    # Update args or environment variables to reflect different number of threads
    if tune_inter_ops:
        param.test_args = test_params.test_args + ["-y", str(lower)]
    elif not is_omp:
        param.test_args = test_params.test_args + ["-x", str(lower)]
    else:
        param.updateEnv({"OMP_NUM_THREADS": str(lower)})
        param.test_args = test_params.test_args + ["-x", "1"]

    run_perf_tuning(param)
    if not param.avg:
        failed_tests.append(param)
        return best_run
    else:
        successful_tests.append(param)
        best_latency = param.avg
        best_run = lower
    
    # Start binary search best thread pool size candidate
    lower += 1
    rerun = False 
    while lower <= upper:
        mid = lower + (upper - lower) // 2
        if mid == 2:
            # Stop the binary search as it have already been run in the previous step
            break
        # Run perf test
        param = PerfTestParams(
            test_params.name + str(mid) + name_suffix,
            test_params.desc + str(mid) + desc_suffix,
            test_params.path,
            [],
            test_params.env_to_set.copy(),
            test_params.args,
            build_name,
        )
        # tune threads by args
        if tune_inter_ops:
            param.test_args = test_params.test_args + ["-y", str(mid)]
        elif not is_omp:
            param.test_args = test_params.test_args + ["-x", str(mid)]
        else:
            param.updateEnv({"OMP_NUM_THREADS": str(mid)})
            # Set "-x 1" to ensure openmp thread pool is used.
            param.test_args = test_params.test_args + ["-x", "1"]
        run_perf_tuning(param)
        if param.avg:
            successful_tests.append(param)
            if best_latency < param.avg:
                upper = mid - 1
            else:
                lower = mid + 1
                best_latency = param.avg
                best_run = mid
        else:
            failed_tests.append(param)
            break
        if lower > upper and best_run == (1 + num_cores) // 2 and not rerun:
            # Re-run search on the first half if the best latency lies in the middle
            upper = (1 + num_cores) // 2 - 1
            lower = 2
            rerun = True
    return best_run

def get_env_var_combos(env_vars):
    if env_vars == None:
        return [[""]]
    import itertools
    env_options_list = env_vars.values()
    all_combos = []
    for options in env_options_list:
        if len(all_combos) == 0:
            all_combos = list(itertools.combinations(options, 1))
        else:
            nested_combo = list(itertools.product(all_combos, options))
            # flatten if necessary
            all_combos = []
            for combo in nested_combo:
                a = []
                for el in combo:
                    if isinstance(el, tuple) or isinstance(el, list) :
                        a.extend(el)
                    else:
                        a.append(el)
                all_combos.append(a)
    return all_combos

# env_names: a list of environment variable names
# env_options: the environment variable value corresponding to the env_names
def gen_env_var_dict(env_names, env_option):
    if len(env_names) == 0 or len(env_option) == 0:
        return {}
    env_var_dict = {}
    for i in range(0, len(env_names)):
        if len(str(env_option[i])) > 0:
            env_var_dict.update({env_names[i]: str(env_option[i])})
    return env_var_dict

def select_graph_optimizer(ep):
    # Placeholder function for graph optimizer
    return ep_graphOptimizer_map.get(ep)

def select_ep(model):
    # Placeholder for selecting appropriate ep
    return model_ep_map.get(model)

class ConverterParamsFromJson():
    def __init__(self):
        with open(parse_arguments().input_json) as f:
            loaded_json = json.load(f)
        cores = os.cpu_count() // 2
        # Check the required inputs
        if loaded_json.get("model") == None:
            raise ValueError("Please specified \"model\" in the input json. ")
        if loaded_json.get("result") == None:
            raise ValueError("Please specified \"result_path\" in the input json. ")
        if loaded_json.get("config") and ["Debug", "MinSizeRel", "Release", "RelWithDebInfo"].index(loaded_json["config"]) == -1:
            raise ValueError("Please specify config with one of the following -\"Debug\", \"MinSizeRel\", \"Release\", \"RelWithDebInfo\"")
        if loaded_json.get("test_mode") and ["duration", "times"].index(loaded_json["test_mode"]) == -1:
            raise ValueError("Please specify mode with one of the following - \"duration\", \"times\"")
        self.model = loaded_json["model"]
        self.result = loaded_json["result"]
        self.config = loaded_json["config"] if loaded_json.get("config") else "RelWithDebInfo"
        self.test_mode = loaded_json["test_mode"] if loaded_json.get("test_mode") else "times"
        self.execution_provider = loaded_json["execution_provider"] if loaded_json.get("execution_provider") else ""
        self.repeated_times = loaded_json["repeated_times"] if loaded_json.get("repeated_times") else "20"
        self.duration_time = loaded_json["duration_time"] if loaded_json.get("duration_time") else "10"
        self.intra_op_num_threads = loaded_json["intra_op_num_threads"] if loaded_json.get("intra_op_num_threads") else str(cores)
        self.inter_op_num_threads = loaded_json["inter_op_num_threads"] if loaded_json.get("inter_op_num_threads") else str(cores)
        self.top_n = loaded_json["top_n"] if loaded_json.get("top_n") else "3"
        self.parallel = loaded_json["parallel"] if loaded_json.get("parallel") else True
        self.optimization_level = loaded_json["optimization_level"] if loaded_json.get("optimization_level") else "99"

def parse_arguments():
    parser = argparse.ArgumentParser()

    cores = os.cpu_count() // 2
    print("Cores: ", cores)
    parser.add_argument("--input_json", 
                        help="A JSON file specifying the run specs. ")
    parser.add_argument("--config", default="RelWithDebInfo",
                        choices=["Debug", "MinSizeRel", "Release", "RelWithDebInfo"],
                        help="Configuration to run.")
    parser.add_argument("-m", "--test_mode", default="times",
                        choices=["duration", "times"],
                        help="Specifies the test mode. Value could be 'duration' or 'times'.")
    parser.add_argument("-e", "--execution_provider", default="",
                        help="Specifies the provider 'cpu','cuda','dnnl' etc.")
    parser.add_argument("-r", "--repeated_times", default="20",
                        help="Specifies the repeated times if running in 'times' test mode. Default:20.")
    parser.add_argument("-t", "--seconds_to_run", default="10",
                        help="Specifies the seconds to run for 'duration' mode. Default:10.")
    parser.add_argument("-x", "--inter_op_num_threads", default=str(cores),
                        help="Sets the number of threads used to parallelize the execution within nodes, \
                        A value of 0 means ORT will pick a default. Must >=0.")
    parser.add_argument("-y", "--intra_op_num_threads", default=str(cores),
                        help="Used when -P is set. Sets the number of threads used to parallelize the execution of the graph (across nodes), \
                        A value of 0 means ORT will pick a default. Must >=0.")
    parser.add_argument("-s", "--top_n", default="3",
                        help="Show percentiles for top n runs in each execution provider. Default:3.")
    parser.add_argument("-P", "--parallel", default=True,
                        help="Use parallel executor instead of sequential executor.")
    parser.add_argument("-o", "--optimization_level", default="99", 
                        help="0: disable optimization, 1: basic optimization, 2: extended optimization, 99: extended+layout optimization.")
    parser.add_argument("--model",
                        help="Model.")
    parser.add_argument("--result",
                        help="Result folder.")

    return parser.parse_args()

if __name__ == "__main__":
    # Get input arguments from either args or json file
    args = parse_arguments()
    if args.input_json != None and len(args.input_json) > 0:
        args = ConverterParamsFromJson()
    else:
        if not args.model or len(args.model) == 0:
            raise ValueError("Please specify the required argument \"model\" either in a json file or by --model")
        if not args.result or len(args.result) == 0:
            raise ValueError("Please specify the required argument \"result\" either in a json file or by --result")

    if not os.path.exists(args.result):
        os.mkdir(args.result)
    
    bin_dir = os.path.join(os.path.dirname(__file__), "bin", args.config)
    build_dirs = os.listdir(bin_dir)

    allProviders = ["cpu_openmp", "mklml", "dnnl", "cpu", "tensorrt", "ngraph", "cuda", "nuphar"]
    # Get all execution providers needed to run in current context
    providers = [p for p in args.execution_provider.split(",") if p != ""] if len(args.execution_provider) > 0 else allProviders
    parallel_eps = ["cpu_openmp", "mklml", "dnnl", "cpu", "ngraph"]
    omp_eps = ["cpu_openmp", "mklml", "dnnl", "ngraph", "nuphar"]

    if len(GPUtil.getGPUs()) == 0:
        print("No GPU found on current device. Cuda and TensorRT performance tuning might not be available. ")
    print("providers ", providers)

    
    # Start perf tuning. Strategy is as follows:
    # 1. Loop for every pre-built execution provider
    # 2. If parallel execution mode is used, binary tune inter_op_num_threads. Pick the best.
    # 3. Loop through all environment variable combinations. 
    # 4. Inside each environment variable combo, 
    #    (1) Binary tune intra_op_num_threads/OMP_NUM_THREADS in sequential execution mode. Pick the best.
    #    (2) With the best picked in step 2 and 4-(1), add the best combo of intra/inter op num threads to perf tuning candidates.
    #    (3) Add the default settings to perf tuning candidates. 
    # 5. Run all candidates and select the top n candidates with best performance for each execution provider. 
    #    Store and profile those candidates.
    successful_by_ep = dict()
    profile_candidates = []
    failed = []
    for build_name in providers:
        if "mklml" in build_name or "nuphar" in build_name:
            build_path = os.path.join(bin_dir, "mklml")
        elif build_name in allProviders:
            build_path = os.path.join(bin_dir, "all_eps")
        else:
            raise ValueError("Provider %s is not currently supported. \
                Please choose one of cpu, dnnl, mklml, cuda, tensorrt, ngraph or nuphar",
                build_name)
        if os.path.isdir(build_path):
            # If current build is requested by user, run perf tuning
            test_args = []
            if "mklml" not in build_name and "cpu_openmp" not in build_name:
                test_args = ["-e", build_name]
            successful = []
            tests = []
            # Tune inter_op_num_threads using parallel execution mode with default environment variables. 
            best_inter_op_num_threads = -1
            if args.parallel and build_name in parallel_eps:                 
                best_inter_op_num_threads = run_perf_tuning_binary(
                    PerfTestParams(
                        build_name + "_parallel_",
                        build_name + " ",
                        build_path,
                        test_args + ["-P"],
                        {},
                        args,
                        build_name,
                    ), int(args.inter_op_num_threads), 
                    "_inter_threads", 
                    " threads, ", failed, successful, False, True)

            env_vars = ep_envvar_map.get(build_name)
            env_var_combos = get_env_var_combos(env_vars)
            env_names = list(env_vars.keys()) if env_vars is not None else []
            # Tune all possible combinations of environment variables, including defaults
            for combo in env_var_combos:
                # generate env var dict {env_var_name: env_var_option}. 
                env = gen_env_var_dict(env_names, combo)
                env_option = ""
                for e in env:
                    env_option += "_" + e + "_" + env.get(e)

                best_thread_pool_size = -1
                is_omp = build_name in omp_eps
                num_threads = int(args.intra_op_num_threads)
                name_suffix = "_intra_threads" if not is_omp else "_OMP_threads"
                desc_suffix = " intra_op_num_threads, " if not is_omp else " OMP_NUM_THREADS, "
                # Tune environment variables and thread pool size using sequential executor
                best_thread_pool_size = run_perf_tuning_binary(
                    PerfTestParams(
                        build_name + "_",
                        build_name + " ",
                        build_path,
                        test_args,
                        env.copy(),
                        args,
                        build_name
                    ), num_threads, name_suffix + env_option, desc_suffix + env_option, failed, successful, is_omp)
                if best_inter_op_num_threads > 1:
                    name_suffix += "_" + str(best_inter_op_num_threads) + "_inter_threads"
                    desc_suffix += str(best_inter_op_num_threads) + " inter_op_num_threads, "
                    # Store the best inter_op_num_threads in test args.
                    if best_thread_pool_size > 1:
                        params = PerfTestParams(
                            build_name + "_parallel_" + str(best_thread_pool_size) + name_suffix + env_option,
                            build_name + " " + str(best_thread_pool_size) + desc_suffix + env_option,
                            build_path,
                            test_args + ["-P", "-y", str(best_inter_op_num_threads)],
                            env.copy(),
                            args,
                            build_name)
                        if is_omp:
                            params.updateEnv({"OMP_NUM_THREADS": str(best_thread_pool_size)})
                            params.test_args += ["-x", "1"]
                        else:
                            params.test_args += ["-x", str(best_thread_pool_size)]
                        tests.append(params)

                # Tune environment variables using sequential executor with default thread settings
                params = PerfTestParams(
                    build_name + env_option,
                    build_name + " " + env_option,
                    build_path,
                    test_args,
                    env.copy(),
                    args,
                    build_name)
                tests.append(params)                    

            # Run the tests under current execution provider.
            for test in tests:
                run_perf_tuning(test)
            
            # keep the best to run profiling
            successful.extend([x for x in tests if x.avg]) 
            successful = sorted(successful, key=lambda e: e.avg)[:int(args.top_n)]
            if len(successful) > 0:
                successful_by_ep[build_name] = successful
                profile_candidates.append(successful_by_ep[build_name][0])
            failed.extend([x for x in tests if not x.avg])

    # Re-run fastest tests in each ep to calculate percentiles.
    for test in profile_candidates:
        run_perf_tuning(test, percentiles=True)
    
    if len(GPUtil.getGPUs()) == 0:
        failed = [x for x in failed if "cuda" not in x.name and "tensorrt" not in x.name]

    # Re-sort tests based on 100 runs
    profile_candidates = sorted(profile_candidates, key=lambda e: e.avg)
    print("")
    print("Results:")
    out_json = []

    # Write performance tuning results to latencies files. 
    with open(os.path.join(args.result, "latencies.txt"), "w") as out_file:
        out_json = collections.OrderedDict()
        # Record successful runs by execution provider                
        for ep_candidate in profile_candidates:                
            json_list = []
            for test in successful_by_ep[ep_candidate.build_name]:
                print(test.name, test.avg, "ms")
                print(test.name, test.avg, "ms", file=out_file)

                json_record = dict()
                json_record["name"] = test.name
                json_record["command"] = test.command
                json_record["avg"] = test.avg
                num_latencies = len(test.latencies)
                if num_latencies >= 10:
                    json_record["p90"] = round(test.latencies[int(num_latencies * .9)] * 1000, 5)
                if num_latencies >= 20:
                    json_record["p95"] = round(test.latencies[int(num_latencies * .95)] * 1000, 5)
                json_record["cpu_usage"] = round(test.cpu / 100, 5)
                json_record["gpu_usage"] = round(test.gpu, 5)
                json_record["memory_util"] = round(test.memory / 100, 5)
                json_record["code_snippet"] = test.gen_code_snippet()
                json_list.append(json_record)
                out_json[test.build_name] = json_list
        # Record failed runs    
        json_list = []
        for test in failed:
            print(test.name, "error")
            print(test.name, "error", file=out_file)
            json_record = dict()
            json_record["name"] = test.name
            json_record["command"] = test.command
            json_record["result"] = "error"
            json_list.append(json_record)
            out_json["failed"] = json_list
    
    with open(os.path.join(args.result, "latencies.json"), "w") as json_file:
        json.dump(out_json, json_file, indent=2)
