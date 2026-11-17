"""The three AI models, loaded once and only when first needed. All three are Apache 2.0 (OK to sell).

- Grounding DINO (IDEA-Research/grounding-dino-tiny): finds things by name ("floor", "sofa") and draws a box.
- SAM 2.1 (facebook/sam2.1-hiera-tiny): turns each box into an exact cut-out (mask) with sharp edges.
- Depth Anything V2 Small (depth-anything/Depth-Anything-V2-Small-hf): how near or far every pixel is.

Swap any of them with the env vars below; nothing else in the code needs to change.
"""
from __future__ import annotations

import os
import threading

import numpy as np
from PIL import Image

DETECTOR = os.getenv("ROOMAI_DETECTOR", "IDEA-Research/grounding-dino-tiny")
SEGMENTER = os.getenv("ROOMAI_SEGMENTER", "facebook/sam2.1-hiera-tiny")
DEPTH = os.getenv("ROOMAI_DEPTH", "depth-anything/Depth-Anything-V2-Small-hf")

_lock = threading.Lock()
_cache: dict = {}


def _get(name, loader):
    with _lock:
        if name not in _cache:
            _cache[name] = loader()
        return _cache[name]


def _torch():
    import torch
    torch.set_num_threads(max(1, os.cpu_count() or 1))
    return torch


def _dev():
    """A graphics card (GPU) when the machine has one, otherwise the normal processor."""
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


def _to(inputs):
    d = _dev()
    return {k: (v.to(d) if hasattr(v, "to") else v) for k, v in inputs.items()}


def detect(img: Image.Image, labels: list[str], box_threshold=0.25, text_threshold=0.2):
    """Boxes for every label found: [(label, score, [x0, y0, x1, y1])]."""
    torch = _torch()
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

    proc, model = _get("det", lambda: (AutoProcessor.from_pretrained(DETECTOR),
                                       AutoModelForZeroShotObjectDetection.from_pretrained(DETECTOR).eval().to(_dev())))
    text = ". ".join(labels) + "."
    inputs = proc(images=img, text=text, return_tensors="pt")
    with torch.no_grad():
        out = model(**_to(inputs))
    res = proc.post_process_grounded_object_detection(
        out, inputs.input_ids.to(_dev()), threshold=box_threshold, text_threshold=text_threshold,
        target_sizes=[img.size[::-1]])[0]
    names = res.get("text_labels") or res.get("labels")
    found = []
    for name, score, box in zip(names, res["scores"].tolist(), res["boxes"].tolist()):
        name = str(name).strip()
        label = next((l for l in labels if l == name), None) or next((l for l in labels if l in name or name in l), name)
        found.append((label, float(score), box))
    return found


def segment(img: Image.Image, boxes: list[list[float]]) -> np.ndarray:
    """One mask per box, shape (n, H, W) bool."""
    if not boxes:
        return np.zeros((0, img.size[1], img.size[0]), bool)
    torch = _torch()
    from transformers import Sam2Model, Sam2Processor

    proc, model = _get("seg", lambda: (Sam2Processor.from_pretrained(SEGMENTER),
                                       Sam2Model.from_pretrained(SEGMENTER).eval().to(_dev())))
    inputs = proc(images=img, input_boxes=[boxes], return_tensors="pt")
    with torch.no_grad():
        out = model(**_to(inputs), multimask_output=False)
    masks = proc.post_process_masks(out.pred_masks.cpu(), inputs["original_sizes"])[0]
    return masks[:, 0].numpy().astype(bool)


def depth(img: Image.Image) -> np.ndarray:
    """Relative disparity (bigger = closer), same size as the image."""
    torch = _torch()
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation

    proc, model = _get("depth", lambda: (AutoImageProcessor.from_pretrained(DEPTH),
                                         AutoModelForDepthEstimation.from_pretrained(DEPTH).eval().to(_dev())))
    inputs = proc(images=img, return_tensors="pt")
    with torch.no_grad():
        pred = model(**_to(inputs)).predicted_depth
    pred = torch.nn.functional.interpolate(pred[:, None], size=img.size[::-1], mode="bicubic", align_corners=False)
    return pred[0, 0].cpu().numpy().astype(np.float64)


def segment_points(img: Image.Image, points: list[list[float]], labels: list[int]) -> np.ndarray:
    """One cut-out from taps: points = [[x, y], ...] in image pixels, labels 1 = "this", 0 = "not this".
    SAM offers three sizes of cut-out for a tap (a part, the object, everything around); we keep the one it trusts most."""
    torch = _torch()
    from transformers import Sam2Model, Sam2Processor

    proc, model = _get("seg", lambda: (Sam2Processor.from_pretrained(SEGMENTER),
                                       Sam2Model.from_pretrained(SEGMENTER).eval().to(_dev())))
    inputs = proc(images=img, input_points=[[points]], input_labels=[[labels]], return_tensors="pt")
    with torch.no_grad():
        out = model(**_to(inputs), multimask_output=True)
    masks = proc.post_process_masks(out.pred_masks.cpu(), inputs["original_sizes"])[0][0]   # (3, H, W)
    best = int(out.iou_scores[0, 0].argmax())
    return masks[best].numpy().astype(bool)
