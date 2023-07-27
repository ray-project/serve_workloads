"""Run Locust on Ray cluster.

Run this script on a Ray cluster's head node to launch one Locust worker per
CPU across the Ray cluster's nodes.

Example command:

$ python locust_runner.py -f locustfile.py -u 200 -r 50 --host [HOST_URL]
"""

import os
import ray
import time
import argparse
import subprocess
from tqdm import tqdm

HTML_RESULTS_DIR = "locust_results"
DEFAULT_RESULT_FILENAME = f"{time.strftime('%Y-%m-%d-%p-%H-%M-%S-%f-results.html')}"

parser = argparse.ArgumentParser()
parser.add_argument(
    "--html",
    default=DEFAULT_RESULT_FILENAME,
    type=str,
    help="HTML file to save results to.",
)

args, locust_args = parser.parse_known_args()

ray.init(address="auto")
num_locust_workers = int(ray.available_resources()["CPU"])
master_address = ray.util.get_node_ip_address()

if not os.path.exists(HTML_RESULTS_DIR):
    os.mkdir(HTML_RESULTS_DIR)

# Required locust args: -f, -u, -r, --host, and any custom locustfile args
base_locust_cmd = [
    "locust",
    "--headless",
    f"--html={HTML_RESULTS_DIR}/{args.html}",
    *locust_args,
]


@ray.remote(num_cpus=1)
class LocustWorker:
    def __init__(self):
        self.proc = None

    def start(self):
        worker_locust_cmd = base_locust_cmd + [
            "--worker",
            f"--master-host={master_address}",
        ]
        self.proc = subprocess.Popen(worker_locust_cmd)


print(f"Spawning {num_locust_workers} Locust worker processes.")

# Hold reference to each locust worker to prevent them from being torn down
locust_workers = []
for _ in tqdm(range(num_locust_workers)):
    locust_worker = LocustWorker.remote()
    ray.get(locust_worker.start.remote())
    locust_workers.append(locust_worker)

master_locust_cmd = base_locust_cmd + [
    "--master",
    f"--expect-workers={num_locust_workers}",
]
proc = subprocess.Popen(master_locust_cmd)
proc.communicate()
