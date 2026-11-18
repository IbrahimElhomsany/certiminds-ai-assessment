"""Download Poly Haven (CC0) models as glTF (2k textures) and one HDRI into assets/."""
import json, os, sys, urllib.request
OUT = "assets"
import subprocess
def get(url): return subprocess.run(["curl", "-sfL", "-m", "300", url], check=True, capture_output=True).stdout
for name in sys.argv[1:]:
    if name.startswith("hdri:"):
        h = name[5:]; f = json.loads(get(f"https://api.polyhaven.com/files/{h}"))
        url = f["hdri"]["4k"]["hdr"]["url"]; p = os.path.join(OUT, h + ".hdr")
        if not os.path.exists(p): open(p, "wb").write(get(url))
        print("hdri", h); continue
    f = json.loads(get(f"https://api.polyhaven.com/files/{name}"))
    g = f["gltf"].get("2k") or f["gltf"]["1k"]; g = g["gltf"]
    d = os.path.join(OUT, name); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, name + ".gltf"), "wb").write(get(g["url"]))
    for rel, inc in g["include"].items():
        p = os.path.join(d, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        if not os.path.exists(p): open(p, "wb").write(get(inc["url"]))
    print("model", name)
