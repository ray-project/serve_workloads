from typing import Optional, Tuple, Dict

from ray.util.metrics import Gauge


class StringGauge(Gauge):
    """A StringGauge maintains a gauge that represents a string value.

    The StringGauge's true value is a string stored in the label_name tag.
    Whenever the StringGauge is updated with a new string, it sets the internal
    gauge's label_name tag to the new string, and it sets the value to 1. It
    sets the previous tagged value to 0.

    Args:
        label_name: The tag key that represents the label value. This should
            also be passed into tag_keys.
        name: Same as Gauge.
        description: Same as Gauge.
        tag_keys: Same as Gauge.
    """

    def __init__(
        self,
        label_name: str,
        name: str,
        description: str = "",
        tag_keys: Optional[Tuple[str]] = None,
    ):
        if label_name not in tag_keys:
            raise ValueError(
                f'label_name "{label_name}" was not in the tag keys that '
                f"were passed in: {tag_keys}. label_name must be one of the "
                "tag_keys passed in."
            )
        super().__init__(name, description, tag_keys)
        self.label_name = label_name
        self.current_value = None

    def set(self, tags: Dict[str, str] = None):
        """Updates the Gauge's value.

        Args:
            value: The string value that the label_name tag should store.
            tags: Same as Gauge. Should contain the label_name tag.
        """
        if self.label_name not in tags:
            raise ValueError(
                f'label_name "{self.label_name}" was not a key in the tags '
                f"that were passed in: {tags}. label_name must be one of the "
                "tag keys passed in."
            )
        if (
            self.current_value is not None
            and self.tags[self.label_name] != self.current_value
        ):
            old_tags = tags.copy()
            old_tags[self.label_name] = self.current_value
            super(Gauge, self).set(0, old_tags)

        super(Gauge, self).set(1, tags)
        self.current_value = tags[self.label_name]
