from typing import Dict


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
