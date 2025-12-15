import os
import urllib.request
import subprocess
import threading
import time
import json
import urllib
import uuid
import websocket
import random
import requests
import shutil
import asyncio
from cog import Path


class ComfyUI:
    def __init__(self, server_address):
        self.server_address = server_address

    def start_server(self, output_directory, input_directory):
        self.input_directory = input_directory
        self.output_directory = output_directory

        start_time = time.time()
        server_thread = threading.Thread(
            target=self.run_server, args=(output_directory, input_directory)
        )
        server_thread.start()
        while not self.is_server_running():
            if time.time() - start_time > 60:
                raise TimeoutError("Server did not start within 60 seconds")
            time.sleep(0.5)

        elapsed_time = time.time() - start_time
        print(f"Server started in {elapsed_time:.2f} seconds")

    def run_server(self, output_directory, input_directory):
        command = f"python ./ComfyUI/main.py --output-directory {output_directory} --input-directory {input_directory} --disable-metadata"

        """
        We need to capture the stdout and stderr from the server process
        so that we can print the logs to the console. If we don't do this
        then at the point where ComfyUI attempts to print it will throw a
        broken pipe error. This only happens from cog v0.9.13 onwards.
        """
        server_process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

        def print_stdout():
            for stdout_line in iter(server_process.stdout.readline, ""):
                print(f"[ComfyUI] {stdout_line.strip()}")

        stdout_thread = threading.Thread(target=print_stdout)
        stdout_thread.start()

        for stderr_line in iter(server_process.stderr.readline, ""):
            print(f"[ComfyUI] {stderr_line.strip()}")

    def is_server_running(self):
        try:
            with urllib.request.urlopen(
                "http://{}/history/{}".format(self.server_address, "123")
            ) as response:
                return response.status == 200
        except urllib.error.URLError:
            return False

    def is_image_or_video_value(self, value):
        filetypes = [".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm"]
        return isinstance(value, str) and any(
            value.lower().endswith(ft) for ft in filetypes
        )

    def handle_inputs(self, workflow):
        print("Checking inputs")
        seen_inputs = set()
        missing_inputs = []
        for node in workflow.values():
            # Skip URLs in LoraLoader nodes
            if node.get("class_type") in ["LoraLoaderFromURL", "LoraLoader"]:
                continue

            if "inputs" in node:
                for input_key, input_value in node["inputs"].items():
                    if isinstance(input_value, str) and input_value not in seen_inputs:
                        seen_inputs.add(input_value)
                        if input_value.startswith(("http://", "https://")):
                            filename = os.path.join(
                                self.input_directory, os.path.basename(input_value)
                            )
                            if not os.path.exists(filename):
                                print(f"Downloading {input_value} to {filename}")
                                try:
                                    response = requests.get(input_value)
                                    response.raise_for_status()
                                    with open(filename, "wb") as file:
                                        file.write(response.content)
                                    print(f"✅ {filename}")
                                except requests.exceptions.RequestException as e:
                                    print(f"❌ Error downloading {input_value}: {e}")
                                    missing_inputs.append(filename)

                            # The same URL may be included in a workflow more than once
                            node["inputs"][input_key] = filename

                        elif self.is_image_or_video_value(input_value):
                            filename = os.path.join(
                                self.input_directory, os.path.basename(input_value)
                            )
                            if not os.path.exists(filename):
                                print(f"❌ {filename} not provided")
                                missing_inputs.append(filename)
                            else:
                                print(f"✅ {filename}")

        if missing_inputs:
            raise Exception(f"Missing required input files: {', '.join(missing_inputs)}")

        print("====================================")

    def connect(self):
        self.client_id = str(uuid.uuid4())
        self.ws = websocket.WebSocket()
        self.ws.connect(f"ws://{self.server_address}/ws?clientId={self.client_id}")

    def post_request(self, endpoint, data=None):
        url = f"http://{self.server_address}{endpoint}"
        headers = {"Content-Type": "application/json"} if data else {}
        json_data = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(
            url, data=json_data, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req) as response:
            if response.status != 200:
                print(f"Failed: {endpoint}, status code: {response.status}")

    # https://github.com/comfyanonymous/ComfyUI/blob/master/server.py
    def clear_queue(self):
        self.post_request("/queue", {"clear": True})
        self.post_request("/interrupt")

    def queue_prompt(self, prompt):
        try:
            # Prompt is the loaded workflow (prompt is the label comfyUI uses)
            p = {"prompt": prompt, "client_id": self.client_id}
            data = json.dumps(p).encode("utf-8")
            req = urllib.request.Request(
                f"http://{self.server_address}/prompt?{self.client_id}", data=data
            )

            output = json.loads(urllib.request.urlopen(req).read())
            return output["prompt_id"]
        except urllib.error.HTTPError as e:
            print(f"ComfyUI error: {e.code} {e.reason}")
            http_error = True

        if http_error:
            raise Exception(
                "ComfyUI Error – Your workflow could not be run. Please check the logs for details."
            )

    def _process_websocket_message(self, message, workflow, prompt_id):
        """Process a websocket message and return True if execution is complete"""
        if message["type"] == "execution_error":
            error_data = message["data"]

            if (
                "exception_message" in error_data
                and "Unauthorized: Please login first to use this node" in error_data["exception_message"]
            ):
                raise Exception("ComfyUI API nodes are not currently supported.")

            error_message = json.dumps(message, indent=2)
            raise Exception(
                f"There was an error executing your workflow:\n\n{error_message}"
            )

        if message["type"] == "executing":
            data = message["data"]
            if data["node"] is None and data["prompt_id"] == prompt_id:
                return True  # Execution complete
            elif data["prompt_id"] == prompt_id:
                node = workflow.get(data["node"], {})
                meta = node.get("_meta", {})
                class_type = node.get("class_type", "Unknown")
                print(
                    f"Executing node {data['node']}, title: {meta.get('title', 'Unknown')}, class type: {class_type}"
                )
        
        return False  # Continue execution

    def wait_for_prompt_completion(self, workflow, prompt_id):
        while True:
            out = self.ws.recv()
            if isinstance(out, str):
                message = json.loads(out)
                if self._process_websocket_message(message, workflow, prompt_id):
                    break

    async def wait_for_prompt_completion_async(self, workflow, prompt_id):
        """Async version of wait_for_prompt_completion that supports cancellation"""
        loop = asyncio.get_running_loop()
        while True:
            # Receive message in a non-blocking way (cancellation is handled by run_in_executor)
            try:
                out = await loop.run_in_executor(None, self.ws.recv)
            except Exception as e:
                raise Exception(f"Error receiving message from websocket: {e}")
            
            if isinstance(out, str):
                message = json.loads(out)
                if self._process_websocket_message(message, workflow, prompt_id):
                    break

    def load_workflow(self, workflow):
        if not isinstance(workflow, dict):
            wf = json.loads(workflow)
        else:
            wf = workflow

        # There are two types of ComfyUI JSON
        # We need the API version
        if any(key in wf.keys() for key in ["last_node_id", "last_link_id", "version"]):
            raise ValueError(
                "You need to use the API JSON version of a ComfyUI workflow. To do this go to your ComfyUI settings and turn on 'Enable Dev mode Options'. Then you can save your ComfyUI workflow via the 'Save (API Format)' button."
            )

        self.handle_inputs(wf)
        return wf

    def reset_execution_cache(self):
        print("Resetting execution cache")
        with open("reset.json", "r") as file:
            reset_workflow = json.loads(file.read())
        self.queue_prompt(reset_workflow)

    def randomise_input_seed(self, input_key, inputs):
        if input_key in inputs and isinstance(inputs[input_key], (int, float)):
            new_seed = random.randint(0, 2**31 - 1)
            print(f"Randomising {input_key} to {new_seed}")
            inputs[input_key] = new_seed

    def randomise_seeds(self, workflow):
        for node_id, node in workflow.items():
            inputs = node.get("inputs", {})
            seed_keys = ["seed", "noise_seed", "rand_seed"]
            for seed_key in seed_keys:
                self.randomise_input_seed(seed_key, inputs)

    def run_workflow(self, workflow):
        print("Running workflow")
        prompt_id = self.queue_prompt(workflow)
        self.wait_for_prompt_completion(workflow, prompt_id)
        output_json = self.get_history(prompt_id)
        print("outputs: ", output_json)
        print("====================================")

    async def run_workflow_async(self, workflow):
        """Async version of run_workflow that supports cancellation"""
        print("Running workflow (async)")
        prompt_id = self.queue_prompt(workflow)
        await self.wait_for_prompt_completion_async(workflow, prompt_id)
        output_json = self.get_history(prompt_id)
        print("outputs: ", output_json)
        print("====================================")

    def get_history(self, prompt_id):
        with urllib.request.urlopen(
            f"http://{self.server_address}/history/{prompt_id}"
        ) as response:
            output = json.loads(response.read())
            return output[prompt_id]["outputs"]

    def get_files(self, directories, prefix="", file_extensions=None):
        files = []
        if isinstance(directories, str):
            directories = [directories]

        for directory in directories:
            for f in os.listdir(directory):
                if f == "__MACOSX":
                    continue
                path = os.path.join(directory, f)
                if os.path.isfile(path):
                    print(f"{prefix}{f}")
                    files.append(Path(path))
                elif os.path.isdir(path):
                    print(f"{prefix}{f}/")
                    files.extend(self.get_files(path, prefix=f"{prefix}{f}/"))

        if file_extensions:
            files = [f for f in files if f.name.split(".")[-1] in file_extensions]

        return sorted(files)

    def cleanup(self, directories):
        self.clear_queue()
        for directory in directories:
            if os.path.exists(directory):
                shutil.rmtree(directory)
            os.makedirs(directory)


