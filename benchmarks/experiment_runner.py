import argparse
from collections import OrderedDict
import copy
import csv
import io
import json
import logging
import os
import subprocess
import sys
import time
import torch
import tiers
import torch_xla.debug.metrics as met
from tqdm import tqdm
from torch.profiler import profile, ProfilerActivity
from torch.autograd import DeviceType
from benchmark_model import ModelLoader
from torchbench_model import TorchBenchModelLoader
from benchmark_experiment import ExperimentLoader
from util import reset_rng_state, move_to_device, randomize_input
import torch_xla.core.xla_model as xm

logger = logging.getLogger(__name__)


class ExperimentRunner:

  def __init__(self, args):
    self._args = args
    self.suite_name = self._args.suite_name

    self.experiment_loader = ExperimentLoader(self._args)

    if self.suite_name == "torchbench":
      self.model_loader = TorchBenchModelLoader(self._args)
    elif self.suite_name == "dummy":
      self.model_loader = ModelLoader(self._args)
    else:
      raise NotImplementedError

    self.output_dir = os.path.abspath(self._args.output_dirname)
    os.makedirs(self.output_dir, exist_ok=True)
    self.output_file = os.path.join(self.output_dir, self._args.output_basename)

  def run(self):
    if self._args.experiment_config and self._args.model_config:
      if self._args.dry_run:
        logger.warning(f"Dry run with {[sys.executable] + sys.argv}")
        return
      experiment_config = json.loads(self._args.experiment_config)
      model_config = json.loads(self._args.model_config)
      self.run_single_experiment(experiment_config, model_config)
    else:
      assert not self._args.experiment_config and not self._args.model_config
      finished_experiments = set()
      if os.path.exists(self.output_file):
        if self._args.no_resume:
          os.unlink(self.output_file)
        else:
          with open(self.output_file, mode="r", encoding="utf-8") as f:
            jsonlines = f.read().splitlines()
          for jsonline in jsonlines:
            tmp = json.loads(jsonline)
            # the finished experiment batch_size may be altered by model set_up(),
            # so the dummy experiment will not match it
            tmp["experiment"]["batch_size"] = self._args.batch_size
            finished_experiments.add("-".join(
                str(item) for item in (list(tmp["model"].values()) +
                                       list(tmp["experiment"].values()))))

      experiment_configs = self.experiment_loader.list_experiment_configs()
      model_configs = self.model_loader.list_model_configs()
      logger.warning(
          f"Number of selected experiment configs: {len(experiment_configs)}")
      logger.warning(f"Number of selected model configs: {len(model_configs)}")
      for model_config in tqdm(
          model_configs,
          desc="model configs",
          disable=not self._args.progress_bar):
        for experiment_config in experiment_configs:
          process_env = experiment_config.pop("process_env")
          experiment_config_str = json.dumps(experiment_config)
          model_config_str = json.dumps(model_config)
          dummy_benchmark_experiment = self.experiment_loader.load_experiment(
              experiment_config)
          dummy_benchmark_model = self.model_loader.load_model(
              model_config, dummy_benchmark_experiment, dummy=True)
          experiment_config["process_env"] = process_env
          command = ([sys.executable] + sys.argv +
                     [f"--experiment-config={experiment_config_str}"] +
                     [f"--model-config={model_config_str}"])
          if self._args.dry_run:
            logger.warning(f"Dry run with {command}")
            continue
          if "-".join(
              str(item)
              for item in (list(dummy_benchmark_model.to_dict().values()) +
                           list(dummy_benchmark_experiment.to_dict().values())
                          )) in finished_experiments:
            continue
          if self.model_loader.is_compatible(dummy_benchmark_model,
                                             dummy_benchmark_experiment):
            try:
              completed_process = subprocess.run(
                  command,
                  timeout=60 * 30,
                  env=process_env,
                  check=True,
                  capture_output=True,
                  encoding="utf-8",
              )
            except subprocess.TimeoutExpired as e:
              logger.error("TIMEOUT")
              self.save_results(dummy_benchmark_experiment,
                                dummy_benchmark_model, {"error": str(e)}, None)
            except subprocess.CalledProcessError as e:
              logger.error("ERROR")
              self.save_results(dummy_benchmark_experiment,
                                dummy_benchmark_model, {"error": e.stderr},
                                None)
            except subprocess.SubprocessError as e:
              logger.error("ERROR")
              self.save_results(dummy_benchmark_experiment,
                                dummy_benchmark_model, {"error": str(e)}, None)
            else:
              if self._args.print_subprocess:
                logger.info(completed_process.stdout)
                logger.warning(completed_process.stderr)

          else:
            e = "SKIP because of incompatible model and experiment configs."
            logger.warning(e)
            self.save_results(dummy_benchmark_experiment, dummy_benchmark_model,
                              {"error": str(e)}, None)

  def run_single_experiment(self, experiment_config, model_config):
    benchmark_experiment = self.experiment_loader.load_experiment(
        experiment_config)
    reset_rng_state(benchmark_experiment)
    benchmark_model = self.model_loader.load_model(model_config,
                                                   benchmark_experiment)

    with benchmark_model.pick_grad():
      metrics = OrderedDict()
      outputs = []
      for i in range(self._args.repeat):
        run_metrics, output = self.timed_run(benchmark_experiment,
                                             benchmark_model)
        output = move_to_device(output, 'cpu')
        outputs.append(output)
        for key, val in run_metrics.items():
          # metrics from repeated runs are formed into lists in the metrics dict
          if key not in metrics:
            metrics[key] = []
          metrics[key].append(val)

    # additional experiment metrics can be added here

    self.save_results(benchmark_experiment, benchmark_model, metrics, outputs)

  def save_results(self, benchmark_experiment, benchmark_model, metrics,
                   outputs):
    if self._args.save_output and outputs is not None:
      outputs_file_name = f"{benchmark_model.filename_str}-{benchmark_experiment.filename_str}.pt"
      torch.save(outputs, os.path.join(self.output_dir, outputs_file_name))
    else:
      outputs_file_name = None

    results = OrderedDict()
    results["model"] = benchmark_model.to_dict()
    results["experiment"] = benchmark_experiment.to_dict()
    results["repeat"] = self._args.repeat
    results["iterations_per_run"] = self._args.iterations_per_run

    results["metrics"] = metrics
    results["outputs_file"] = outputs_file_name

    self.output_jsonl(results)

  def output_jsonl(self, obj, file_path=None):
    if not file_path:
      file_path = self.output_file
    json_str = json.dumps(obj, ensure_ascii=False)
    with open(file_path, mode="a", encoding="utf-8") as f:
      f.write(f"{json_str}\n")

  def output_csv(self, headers, row, file_path=None):
    if not file_path:
      file_path = self.output_file
    existed = os.path.exists(file_path)
    output = csv.writer(
        io.TextIOWrapper(
            open(file_path, "ab", buffering=0),
            "utf-8",
            write_through=True,
        ),
        lineterminator="\n",
    )
    if not existed:
      output.writerow(headers)
    output.writerow([(f"{x:.8e}" if isinstance(x, float) else x) for x in row])

  def _mark_step(self, benchmark_experiment):
    if benchmark_experiment.xla:
      xm.mark_step()

  def _synchronize(self, benchmark_experiment):
    if benchmark_experiment.xla:
      xm.wait_device_ops()
    elif benchmark_experiment.accelerator == "cuda":
      torch.cuda.synchronize()
    else:
      pass

  def prepare_inputs(self, example_inputs, should_randomize_input):
    inputs_list = []
    for i in range(self._args.iterations_per_run):
      inputs = copy.deepcopy(example_inputs)
      if should_randomize_input:
        inputs = randomize_input(inputs)
      inputs_list.append(inputs)
    return inputs_list

  def dump_profile_info(self, prof, benchmark_model, benchmark_experiment):
    assert prof is not None, 'Expecting profiler to be defined!'
    if not self._args.profile_cuda_dump:
      logger.warning(
          'Profiling enabled, but dumping tracing/kernel summary disabled.')
      return

    create_prof_filename = lambda name, ext: f"ptprofile-{name}-{benchmark_model.filename_str}-{benchmark_experiment.filename_str}.{ext}"
    model_name = benchmark_model.model_name
    file_path = os.path.join(self._args.profile_cuda_dump, model_name)
    os.makedirs(file_path, exist_ok=True)
    prof.export_chrome_trace(
        os.path.join(file_path, create_prof_filename("trace", "json")))

    kernel_dump = prof.key_averages().table(
        sort_by="cuda_time_total", row_limit=500)
    with open(
        os.path.join(file_path, create_prof_filename("kernel_dump", "txt")),
        "a") as f:
      f.write(kernel_dump)

  def collect_profile_to_metrics(self, prof, metrics):
    assert prof is not None, 'Expecting profiler to be defined!'
    if not self._args.profile_cuda_cpu_collect:
      logger.warning(
          'Profiling enabled, but collection of CPU/CUDA profiling info disabled.'
      )
      return

    kernel_dump = prof.profiler.total_average()
    total_cuda_time = 0
    total_cpu_time = kernel_dump.self_cpu_time_total

    # Avoid double counting CUDA time for inductor. Copied here, since the interface is not really exposed via any interface.
    # The alternative is regex matching resulting string dump for CUDA kernel time.
    # Source: https://github.com/pytorch/pytorch/blob/2f3beb715c608a060934c237de402faa40ea211f/torch/autograd/profiler_util.py#L1025-L1037
    for evt in prof.profiler.key_averages():
      if evt.device_type == DeviceType.CPU:
        # in legacy profiler, kernel info is stored in cpu events
        if evt.is_legacy:
          if not use_device:
            total_cuda_time += evt.self_cuda_time_total
      elif evt.device_type == DeviceType.CUDA:
        # in kineto profiler, there're events with the correct device type (e.g. CUDA)
        total_cuda_time += evt.self_cuda_time_total

    total_cpu_time /= 1000000
    total_cuda_time /= 1000000
    metrics["total_cpu_time_s"] = total_cpu_time
    metrics["total_cuda_time_s"] = total_cuda_time
    metrics[
        "per_iter_cpu_time_s"] = total_cpu_time / self._args.iterations_per_run
    metrics[
        "per_iter_cuda_time_s"] = total_cuda_time / self._args.iterations_per_run

  def get_xla_cpu_fallback_ops(self, met):
    return set(name for name in met.counter_names() if self.is_aten_op(name))

  def is_aten_op(self, op_name):
    return 'aten::' in op_name

  def collect_individual_ops(self, benchmark_experiment, metrics, prof):
    assert prof is not None, 'Expecting prof to be defined!'

    us_to_s = lambda x: x / 1000000
    extract_prof_info = lambda event: {
        "self_cpu_time_s": us_to_s(event.self_cpu_time_total),
        "self_cuda_time_s": us_to_s(event.self_cuda_time_total),
        "total_cpu_time_s": us_to_s(event.cpu_time_total),
        "total_cuda_time_s": us_to_s(event.cuda_time_total),
        "num_of_calls": event.count
    }

    if benchmark_experiment.xla:
      unlowered_ops = self.get_xla_cpu_fallback_ops(met)
      if not unlowered_ops:
        return
      if "xla_unlowered_ops" not in metrics:
        metrics["xla_unlowered_ops"] = dict()
      for event in prof.key_averages():
        if event.key in unlowered_ops:
          metrics["xla_unlowered_ops"][event.key] = extract_prof_info(event)
    else:
      for event in prof.key_averages():
        op_name = event.key
        if not self.is_aten_op(op_name):
          continue
        if "inductor_ops" not in metrics:
          metrics["inductor_ops"] = dict()
        metrics["inductor_ops"][op_name] = extract_prof_info(event)

  def timed_run(self, benchmark_experiment, benchmark_model):
    reset_rng_state(benchmark_experiment)

    inputs_list = self.prepare_inputs(benchmark_model.example_inputs,
                                      self._args.randomize_input)

    reset_rng_state(benchmark_experiment)
    self._mark_step(benchmark_experiment)
    self._synchronize(benchmark_experiment)

    enable_prof = self._args.profile_cuda
    metrics = OrderedDict()
    t_start = time.perf_counter()
    if benchmark_experiment.xla:
      t_trace = 0

    def loop(prof=None):
      nonlocal t_trace
      for i in range(self._args.iterations_per_run):
        if benchmark_experiment.xla:
          t_trace_start = time.perf_counter()

        output = benchmark_model.model_iter_fn(
            inputs_list[i], collect_full_output=self._args.collect_full_output)

        if benchmark_experiment.xla:
          t_trace += time.perf_counter() - t_trace_start

        self._mark_step(benchmark_experiment)

        if prof:
          prof.step()
      self._synchronize(benchmark_experiment)
      return output

    if enable_prof:
      with profile(
          activities=[ProfilerActivity.CUDA, ProfilerActivity.CPU]) as prof:
        output = loop(prof)
    else:
      output = loop()

    t_end = time.perf_counter()
    if enable_prof:
      self.dump_profile_info(prof, benchmark_model, benchmark_experiment)
      self.collect_profile_to_metrics(prof, metrics)

    metrics["total_time"] = t_end - t_start
    metrics[
        "per_iter_time"] = metrics["total_time"] / self._args.iterations_per_run
    if benchmark_experiment.xla:
      metrics["trace_per_iter_time"] = t_trace / self._args.iterations_per_run

    if enable_prof:
      self.collect_individual_ops(benchmark_experiment, metrics, prof)

    return metrics, output


def parse_args(args=None):
  parser = argparse.ArgumentParser()

  parser.add_argument(
      "--suite-name",
      required=True,
      choices=["dummy", "torchbench"],
      help="Suite name for the model garden.",
  )

  parser.add_argument(
      "--filter",
      "-k",
      action="append",
      default=[],
      help="filter benchmarks with regexp")

  parser.add_argument(
      "--exclude",
      "-x",
      action="append",
      default=[],
      help="filter out benchmarks with regexp")

  parser.add_argument(
      "--filter-by-tier",
      type=int,
      action="append",
      default=[],
      help="filter benchmarks by predefined tier 1-3",
  )

  parser.add_argument(
      "--exclude-by-tier",
      type=int,
      action="append",
      default=[],
      help="filter out benchmarks by predefined tier 1-3",
  )

  def parse_log_level(level: str):
    level = level.lower()
    if level == "critical":
      return logging.CRITICAL
    elif level == "error":
      return logging.ERROR
    elif level == "warning":
      return logging.WARNING
    elif level == "info":
      return logging.INFO
    elif level == "debug":
      return logging.DEBUG
    else:
      raise NotImplementedError

  parser.add_argument(
      "--log-level",
      default="info",
      choices=[
          logging.CRITICAL,
          logging.ERROR,
          logging.WARNING,
          logging.INFO,
          logging.DEBUG,
      ],
      type=parse_log_level,
      help="Specify log level.",
  )

  parser.add_argument(
      "--accelerator",
      choices=["cpu", "cuda", "tpu"],
      action="append",
      help="Specify an accelerator to use.",
  )

  parser.add_argument(
      "--xla",
      choices=["None", "PJRT", "XRT"],
      action="append",
      help="Specify an xla option to use.",
  )

  parser.add_argument(
      "--dynamo",
      choices=["None", "inductor", "openxla_eval", "openxla"],
      action="append",
      help="Specify an xla option to use.",
  )

  parser.add_argument(
      "--test",
      choices=["eval", "train"],
      action="append",
      help="Specify a test to run.",
  )

  parser.add_argument(
      "--repeat",
      type=int,
      default=10,
      help="Number of times to repeat the timed run in a single experiment.",
  )

  parser.add_argument(
      "--iterations-per-run",
      type=int,
      default=1,
      help="Number of times to repeat the model iteration inside a timed run.",
  )

  parser.add_argument(
      "--batch-size",
      type=int,
      help="Batch size to be used. If not provided, it depends on the model suites to determine it.",
  )

  parser.add_argument(
      "--total-partitions",
      type=int,
      default=1,
      choices=range(1, 10),
      help="Total number of partitions we want to divide the benchmark suite into",
  )

  parser.add_argument(
      "--partition-id",
      type=int,
      default=0,
      help="ID of the benchmark suite partition to be run. Used to divide CI tasks",
  )

  parser.add_argument(
      "--dry-run",
      action="store_true",
      help="Do a dry run to only print the benchmark commands.",
  )

  parser.add_argument(
      "--print-subprocess",
      action="store_true",
      help="Print subprocess stdout.",
  )

  parser.add_argument(
      "--progress-bar",
      action="store_true",
      help="Display progress bar.",
  )

  parser.add_argument(
      "--randomize-input",
      action="store_true",
      help="Whether to randomize the input values. Dimensions will be kept the same.",
  )

  parser.add_argument(
      "--collect-full-output",
      action="store_true",
      help="""Whether to collect full output for training. Set this to true if we
        want to verify the numerical correctness of gradients. But that may
        cause time measurement not accurate""",
  )

  parser.add_argument(
      "--save-output",
      action="store_true",
      help="Whether to save the model output to disk",
  )

  parser.add_argument(
      "--output-dirname",
      type=str,
      default="./output/",
      help="Overrides the directory to place output files.",
  )

  parser.add_argument(
      "--output-basename",
      type=str,
      default="results.jsonl",
      help="Overrides the basename of output files.",
  )

  parser.add_argument(
      "--no-resume",
      action="store_true",
      help="""By default, the runner would skip the finished experiments that
        exist in the output-basename file. If --no-resume is set, the previous
        output-basename file will be deleted and all experiment will run""",
  )

  parser.add_argument(
      "--profile-cuda",
      action="store_true",
      help="""Whether to profile CUDA or not. Note this does not do much except for
      triggering a profiler. To get the profiling data use additionally --profile-cuda-dump""",
  )

  parser.add_argument(
      "--profile-cuda-dump",
      type=str,
      default="",
      help="Directory specifying where to dump profiling information (summary, and trace)",
  )

  parser.add_argument(
      "--profile-cuda-cpu-collect",
      action="store_true",
      help="Whether to collect CPU/GPU profiling information in the resulting file.",
  )

  parser.add_argument(
      "--xla-flags",
      type=str,
      action="append",
      help="Flags to forward to XLA via `XLA_FLAGS` env var.",
  )

  parser.add_argument(
      "--disable-tf32",
      action="store_true",
      default=False,
      help="Whether to enable fast F32 multiplication in PyTorch.",
  )

  parser.add_argument(
      "--experiment-config",
      type=str,
      help="JSON string defining the experiment configuration. When set an experiment is run with exactly this one configuration.",
  )

  parser.add_argument(
      "--model-config",
      type=str,
      help="JSON string defining the model configuration. When set an experiment is run with exactly this one configuration.",
  )

  return parser.parse_args(args)


def main():
  args = parse_args()

  # Expand filter/exclude by tier.
  tiers.append_filter_by_tier(args.filter, args.filter_by_tier)
  tiers.append_filter_by_tier(args.exclude, args.exclude_by_tier)
  args.filter = args.filter or [r"."]
  args.exclude = args.exclude or [r"^$"]

  logging.basicConfig(level=args.log_level, force=True)

  logger.info(args)

  if not args.disable_tf32:
    logger.warning('Enabling fast F32 multiplication for PyTorch')
    torch.set_float32_matmul_precision('high')

  runner = ExperimentRunner(args)
  runner.run()


if __name__ == "__main__":
  main()
