from typing import Dict

from ray import serve


@serve.deployment(
    autoscaling_config={
        "min_replicas": 1,
        "max_replicas": 3,
        "target_num_ongoing_requests_per_replica": 15,
    },
    user_config={"use_gpu": False, "batch_size": 4, "image_size": (800, 800)},
    ray_actor_options={
        "num_cpus": 0,
        # There should be 1 node_singleton per node to ensure each node has
        # 1 replica.
        "resources": {
            "node_singleton": 1,
        },
    },
)
class DetrHost:
    def __init__(self):
        from transformers import DetrImageProcessor, DetrForObjectDetection
        import torch

        self.torch = torch

        # TODO (shrekris-anyscale): Cache model weights
        self.model_weights_path = "facebook/detr-resnet-101"
        self.processor = DetrImageProcessor.from_pretrained(self.model_weights_path)
        self.model = DetrForObjectDetection.from_pretrained(self.model_weights_path)

    def reconfigure(self, config: Dict):
        self.use_gpu = config.get("use_gpu", False)
        self.batch_size = config.get("batch_size", 4)
        self.image_size = config.get("image_size", (800, 800))

    def run_model(self, images):
        with self.torch.no_grad():
            inputs = self.processor.preprocess(
                images=[image for image in images], return_tensors="pt"
            )
            pixel_values = inputs["pixel_values"]
            pixel_mask = inputs["pixel_mask"]
            if self.use_gpu:
                pixel_values.cuda()
                pixel_mask.cuda()
            outputs = self.model(pixel_values=pixel_values, pixel_mask=pixel_mask)
            results = self.processor.post_process_object_detection(
                outputs,
                target_sizes=self._cuda(
                    self.torch.tensor([self.image_size] * len(images))
                ),
            )
            labels = results["labels"]
            return labels

    def _cuda(self, tensor_or_model):
        """Helper function that places model or tensors on cuda if GPU enabled."""

        if self.use_gpu:
            return tensor_or_model.cuda()
        else:
            return tensor_or_model
