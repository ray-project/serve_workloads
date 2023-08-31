import json
import base64
import requests


url = "http://localhost:8000/"
headers = {"Content-type": "application/json"}
image_file = "problem.png"

with open(image_file, "rb") as f:
    image_bytes = f.read()

image_bytes_utf8: str = base64.b64encode(image_bytes).decode("utf8")

payload = json.dumps({"image_bytes_utf8": image_bytes_utf8})
response = requests.post(url, data=payload, headers=headers)
data = response.json()

print(data)
