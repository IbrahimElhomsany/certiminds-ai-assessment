"""Run the room AI on Modal (modal.com): a computer with a graphics card that you only pay for while it works.

One-time setup (on your laptop, in the repo folder):
    pip install modal
    modal setup                                   # opens the browser to log in
    # in the Modal website: Secrets -> Create -> Custom, name it  nova-roomai,
    # with one key  ROOMAI_TOKEN  = a long random password you make up
    modal deploy backend/roomai/modal_app.py      # prints the web address of the service

Then in Render (nova-studio -> Environment) add:
    ROOMAI_URL    = the address Modal printed (ends with .modal.run)
    ROOMAI_TOKEN  = the same password as in the Modal secret

Cost: the machine starts when a photo arrives (about 20-40 s the first time), answers in a few seconds,
and switches itself off after SCALEDOWN seconds of quiet. A T4 card costs about 0.6 US$ per hour of
running, so roughly 1-2 US cents per customer photo. Modal's free monthly credit covers about 30 US$.
"""
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
SCALEDOWN = 60                       # seconds of quiet before the machine switches off (and stops costing)


def _download():
    """Bake the three models into the image, so a cold start doesn't download 0.5 GB every time."""
    from transformers import (AutoImageProcessor, AutoModelForDepthEstimation, AutoModelForZeroShotObjectDetection,
                              AutoProcessor, Sam2Model, Sam2Processor)
    AutoProcessor.from_pretrained("IDEA-Research/grounding-dino-tiny")
    AutoModelForZeroShotObjectDetection.from_pretrained("IDEA-Research/grounding-dino-tiny")
    Sam2Processor.from_pretrained("facebook/sam2.1-hiera-tiny")
    Sam2Model.from_pretrained("facebook/sam2.1-hiera-tiny")
    AutoImageProcessor.from_pretrained("depth-anything/Depth-Anything-V2-Small-hf")
    AutoModelForDepthEstimation.from_pretrained("depth-anything/Depth-Anything-V2-Small-hf")


image = (
    modal.Image.debian_slim(python_version="3.12")
    # the same versions that were tested (torchvision is needed by SAM 2's image preparation)
    .pip_install("torch==2.15.0", "torchvision==0.29.2", "transformers==5.23.0", "scipy==1.18.1",
                 "numpy==2.5.5", "pillow==13.0.0", "fastapi[standard]")
    .env({"HF_HOME": "/models"})
    .run_function(_download)
    .run_commands("mkdir -p /root/backend && touch /root/backend/__init__.py")
    .add_local_dir(HERE, "/root/backend/roomai", ignore=["__pycache__", "*.pyc"])
)

app = modal.App("nova-roomai", image=image)


@app.function(gpu="T4", secrets=[modal.Secret.from_name("nova-roomai")], scaledown_window=SCALEDOWN,
              timeout=300, max_containers=2)
@modal.asgi_app()
def web():
    from backend.roomai.service import app as service
    return service
