#!/usr/bin/env python3
"""render_view <file.step> [out.png]  |  validate_cad_model <file.step>

validate_cad_model loads the STEP file with build123d and writes <file>.metrics.json with volume,
bounding box and sampled surface points: the format verifiers/cad.py compares to the reference.
"""
import json
import os
import sys


def validate(path):
    from build123d import import_step
    shape = import_step(path)
    bb = shape.bounding_box()
    pts = [list(v.to_tuple()) for v in shape.vertices()][:500]
    metrics = {"volume": shape.volume, "bbox": [bb.size.X, bb.size.Y, bb.size.Z], "surface_points": pts,
               "valid": shape.is_valid()}
    with open(path + ".metrics.json", "w") as f:
        json.dump(metrics, f)
    print(json.dumps({k: metrics[k] for k in ("volume", "bbox", "valid")}))


def render(path, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from build123d import import_step
    shape = import_step(path)
    pts = [v.to_tuple() for v in shape.vertices()]
    fig = plt.figure(figsize=(8, 8))
    for i, (elev, azim) in enumerate([(90, -90), (0, -90), (0, 0), (30, -60)]):
        ax = fig.add_subplot(2, 2, i + 1, projection="3d")
        ax.scatter(*zip(*pts), s=2)
        ax.view_init(elev, azim)
    fig.savefig(out)
    print(out)


if __name__ == "__main__":
    cmd = os.path.basename(sys.argv[0])
    if cmd == "render_view":
        render(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else sys.argv[1] + ".png")
    else:
        validate(sys.argv[1])
