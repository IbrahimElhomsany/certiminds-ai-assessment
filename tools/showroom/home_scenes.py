"""Turns the showroom's flat display pictures into the shop's built-in home page (data/homepage.json).

Run after: python tools/showroom/build_showroom.py --assets <dir> --views
which renders images/home/show_<view>.jpg and writes images/home/views.json (the dots, worked out by projecting
each product's spot in the 3D showroom into the picture and checking nothing stands in front of it).
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = os.path.join(ROOT, "images", "home")
views = json.load(open(os.path.join(HOME, "views.json")))
scenes = [{"id": vid, "title": v["title"], "image": v["image"], "made": "render", "spots": v["spots"]}
          for vid, v in views.items() if os.path.exists(os.path.join(HOME, f"show_{vid}.jpg"))]
out = {"version": 2, "film": None, "scenes": scenes}
with open(os.path.join(ROOT, "data", "homepage.json"), "w") as f:
    f.write("{\n  \"version\": 2,\n  \"film\": null,\n  \"scenes\": [\n")
    f.write(",\n".join("    " + json.dumps(s) for s in scenes))
    f.write("\n  ]\n}\n")
print(len(scenes), "scenes")
