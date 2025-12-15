import os
import asyncio
from typing import List
from cog import BasePredictor, Input, Path
from comfyui import ComfyUI
import requests
import base64


os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

OUTPUT_DIR = "/tmp/outputs"
INPUT_DIR = "/tmp/inputs"
COMFYUI_TEMP_OUTPUT_DIR = "ComfyUI/temp"
ALL_DIRECTORIES = [OUTPUT_DIR, INPUT_DIR, COMFYUI_TEMP_OUTPUT_DIR]

# Use reset.json as default example workflow if examples directory doesn't exist
try:
    with open("examples/api_workflows/birefnet_api.json", "r") as file:
        EXAMPLE_WORKFLOW_JSON = file.read()
except FileNotFoundError:
    with open("reset.json", "r") as file:
        EXAMPLE_WORKFLOW_JSON = file.read()


class Predictor(BasePredictor):
    def setup(self):
        for directory in ALL_DIRECTORIES:
            os.makedirs(directory, exist_ok=True)

        self.comfyUI = ComfyUI("127.0.0.1:8188")
        self.comfyUI.start_server(OUTPUT_DIR, INPUT_DIR)

    async def predict(
        self,
        workflow_json: str = Input(
            description="Your ComfyUI workflow as JSON string or URL. You must use the API version of your workflow.",
            default="",
        ),
    ) -> List[Path]:
        """Run a single prediction on the model"""
        try:
            self.comfyUI.cleanup(ALL_DIRECTORIES)

            workflow_json_content = workflow_json
            if workflow_json.startswith("data:") and ";base64," in workflow_json:
                try:
                    base64_part = workflow_json.split(",", 1)[1]
                    decoded_bytes = base64.b64decode(base64_part)
                    workflow_json_content = decoded_bytes.decode("utf-8")
                except Exception as e:
                    raise ValueError(f"Failed to decode base64 workflow JSON: {e}")
            elif workflow_json.startswith(("http://", "https://")):
                try:
                    response = requests.get(workflow_json)
                    response.raise_for_status()
                    workflow_json_content = response.text
                except requests.exceptions.RequestException as e:
                    raise ValueError(f"Failed to download workflow JSON from URL: {e}")

            wf = self.comfyUI.load_workflow(workflow_json_content or EXAMPLE_WORKFLOW_JSON)

            self.comfyUI.connect()
            self.comfyUI.randomise_seeds(wf)
            
            # Run workflow in async context with cancellation support
            await self.comfyUI.run_workflow_async(wf)

            return self.comfyUI.get_files(OUTPUT_DIR)
        except asyncio.CancelledError:
            print("Prediction cancelled, interrupting ComfyUI workflow...")
            self.comfyUI.clear_queue()
            raise
