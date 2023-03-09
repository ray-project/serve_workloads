from enum import Enum

RECEIVER_KILL_KEY = "kill_node"


class KillOptions(str, Enum):
    KILL = "kill"
    SPARE = "spare"
