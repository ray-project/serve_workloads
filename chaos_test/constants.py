from enum import Enum

RECEIVER_KILL_KEY = "kill_node"


class KillOptions(str, Enum):
    RAY_STOP = "ray_stop"
    NODE_HALT = "node_halt"
    SPARE = "spare"

    @classmethod
    def kill_types(cls):
        """Returns a list of enum options that kill a node."""
        return [cls.RAY_STOP, cls.NODE_HALT]


class ServiceStatus(str, Enum):
    ROLLOUT_INITIATED = "Rollout initiated"
    ROLLBACK_INITIATED = "Rollback initiated"
    ROLLOUT_UPDATED = "Rollout updated"
    CREATE_INITIATED = "Create initiated"
    RESTART_INITIATED = "Restart initiated"
    TERMINATE_INITIATED = "Terminate initiated"
    UPDATE_INITIATED = "Update initiated"
    RUNNING = "Running"
    UNHEALTHY = "Unhealthy"
    SYSTEM_FAILURE = "System failure"
    STARTING = "Starting"
    TERMINATED = "Terminated"
    TERMINATING = "Terminating"
    UPDATING = "Updating"
    ROLLING_OUT = "Rolling out"
    ROLLINGBACK = "Rolling back"


RECEIVER_CONFIG_FILENAME = "receiver_config.yaml"
