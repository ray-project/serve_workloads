import logging
from typing import Tuple

from fastapi import FastAPI, UploadFile

from ray import serve
from ray.serve.handle import RayServeHandle


logger = logging.getLogger("ray.serve")


fastapi_app = FastAPI()


@serve.deployment(ray_actor_options={"num_cpus": 0})
@serve.ingress(fastapi_app)
class ProblemOrchestrator:
    def __init__(
        self,
        reader_handle: RayServeHandle,
        solver_handle: RayServeHandle,
    ):
        self.reader_handle = reader_handle
        self.solver_handle = solver_handle

    @fastapi_app.post("/")
    async def process_image(self, image_file: UploadFile) -> Tuple[str, float]:
        problem_text = await (await self.reader_handle.remote(image_file))
        symbol, value = await (await self.solver_handle.remote(problem_text))
        return symbol, value


@serve.deployment(ray_actor_options={"num_gpus": 1})
class ProblemReader:
    def __init__(self):
        # Suppress TensorFlow and HuggingFace warnings
        import os

        os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
        os.environ["TRANSFORMERS_VERBOSITY"] = "error"

        from transformers import TrOCRProcessor, VisionEncoderDecoderModel

        self.MODEL_ID = "microsoft/trocr-small-printed"
        self.processor = TrOCRProcessor.from_pretrained(self.MODEL_ID)
        self.model = VisionEncoderDecoderModel.from_pretrained(self.MODEL_ID).cuda()
        self.model.eval()

    async def __call__(self, image_file: UploadFile) -> str:
        import torch
        from PIL import Image

        logger.info(f"Received file: {image_file.filename}")

        image = Image.open(image_file.file).convert("RGB")

        with torch.no_grad():
            pixel_values = self.processor(
                image, return_tensors="pt"
            ).pixel_values.cuda()
            generated_ids = self.model.generate(pixel_values)
            generated_text = self.processor.batch_decode(
                generated_ids, skip_special_tokens=True
            )[0]

            logger.info(f"Generated text: {generated_text}")

            return generated_text


@serve.deployment
class ProblemSolver:
    def __init__(self):
        from sympy.parsing.sympy_parser import (
            standard_transformations,
            implicit_multiplication_application,
        )

        self.transformations = standard_transformations + (
            implicit_multiplication_application,
        )

    def __call__(self, problem: str) -> Tuple[str, float]:
        from sympy.solvers import solve_linear
        from sympy.parsing.sympy_parser import parse_expr

        lhs_str, rhs_str = problem.split("=")

        lhs_expr = parse_expr(
            lhs_str, transformations=self.transformations, evaluate=False
        )
        rhs_expr = parse_expr(
            rhs_str, transformations=self.transformations, evaluate=False
        )

        symbol, value = solve_linear(lhs=lhs_expr, rhs=rhs_expr)
        return str(symbol), float(value)


app = ProblemOrchestrator.bind(ProblemReader.bind(), ProblemSolver.bind())
