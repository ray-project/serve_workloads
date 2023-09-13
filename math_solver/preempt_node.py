import sys
import ray
import json
import base64
import subprocess


if len(sys.argv) != 2:
    raise ValueError(
        f"Got {len(sys.argv) - 1} arguments for Python script. Expected only "
        "1 argument: the node id of the node to preempt. Please pass in the "
        "node id. Run this script by running "
        "`python preempt_node.py [NODE_ID]`."
    )

ray.init(address="auto")

hex_node_id = sys.argv[1]

b64_node_id = base64.b64encode(ray.NodeID.from_hex(hex_node_id).binary()).decode(
    "ascii"
)

subprocess.run(
    [
        "./grpcurl",
        "-d",
        json.dumps(
            {"node_id": b64_node_id, "reason": 2, "reason_message": "preemption"}
        ),
        "-plaintext",
        "127.0.0.1:6379",
        "ray.rpc.autoscaler.AutoscalerStateService.DrainNode",
    ],
    capture_output=True,
    text=True,
)
