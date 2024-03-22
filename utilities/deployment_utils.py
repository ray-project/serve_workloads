import yaml
from pathlib import Path
from typing import Dict

from chaos_test.constants import RECEIVER_CONFIG_FILENAME


class BaseReconfigurableDeployment:
    """Contains a boilerplate reconfigure method."""

    def __init__(self, config_options):
        """Initializes the object.
        
        Args:
            config_options: maps all the allowed config options for this class
                to the corresponding type_cast function. E.g.:

                {
                    "threshold": float,
                    "id": str,
                    "sub_config": dict,
                }
        """

        self.config_options = config_options

    def reconfigure(self, config: Dict) -> None:
        """Reconfigures the deployment using values from config.

        For every key-value pair in config, this function updates the
        corresponding attribute with that value. E.g.:

        config = {"hello", "world"} --> self.hello == "world"
        """
        for option, value in config.items():
            if option not in self.config_options:
                print(
                    f'Ignoring invalid option "{option}" in config. Valid '
                    f"options are: {list(self.config_options.keys())}"
                )
            else:
                type_cast = self.config_options[option]
                new_value = type_cast(value)
                if hasattr(self, option) and getattr(self, option) != new_value:
                    print(
                        f'Changing {option} from "{getattr(self, option)}" to "{new_value}"'
                    )
                else:
                    print(f'Initializing {option} to "{new_value}"')
                setattr(self, option, new_value)


def get_receiver_serve_config(receiver_serve_config_dir: str) -> Dict:
    """Gets the Serve config for the Receiver application.
    
    Args:
        receiver_serve_config_dir: directory that contains the Receiver's
            Serve config.
    """

    aliased_path_prefix = "/tmp/ray/session_latest/runtime_resources"
    aliased_dir = aliased_path_prefix + receiver_serve_config_dir.split("runtime_resources")[1]
    receiver_config_file_path = f"{aliased_dir}/{RECEIVER_CONFIG_FILENAME}"
    print(f'Using Receiver config at "{receiver_config_file_path}"')
    with open(receiver_config_file_path) as f:
        receiver_serve_config = yaml.safe_load(f)

    return receiver_serve_config
