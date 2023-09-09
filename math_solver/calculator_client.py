import requests


url = "http://localhost:8000/"
image_file = "problem.png"

with open(image_file, "rb") as f:
    files = {"image_file": f}
    response = requests.post(url, files=files)

data = response.json()
print(data)
