from transformers import TrOCRProcessor, VisionEncoderDecoderModel


model_ids = [
    "microsoft/trocr-small-printed",
    "microsoft/trocr-base-printed",
]

for model_id in model_ids:
    # Cache model by constructing processor and model
    processor = TrOCRProcessor.from_pretrained(model_id)
    model = VisionEncoderDecoderModel.from_pretrained(model_id)
