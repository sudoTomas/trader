"""Draws the Salamander mark: an S-shaped fire salamander seen from above.

The body is a variable-width shape around a cubic centreline (head bulb, neck, torso,
tapering tail), legs sit perpendicular to the body at shoulder and hip, and the
fire-salamander spots sit on the spine. Run it to regenerate brand/salamander-mark*.svg.
"""

import math

# Centreline: head at top right, S-curve down, tail sweeping out bottom left.
SEGS = [
    ((64, 16), (46, 8), (22, 20), (32, 38)),
    ((32, 38), (40, 52), (70, 50), (68, 68)),
    ((68, 68), (66, 84), (44, 90), (24, 82)),
]


def bez(p, t):
    a, b, c, d = p
    mt = 1 - t
    x = mt**3 * a[0] + 3 * mt * mt * t * b[0] + 3 * mt * t * t * c[0] + t**3 * d[0]
    y = mt**3 * a[1] + 3 * mt * mt * t * b[1] + 3 * mt * t * t * c[1] + t**3 * d[1]
    return x, y


def centreline(n=240):
    pts = []
    for seg in SEGS:
        for k in range(n // len(SEGS)):
            pts.append(bez(seg, k / (n // len(SEGS))))
    pts.append(SEGS[-1][3])
    return pts


def width(s):
    """Half-width along the body, s in 0..1 from snout to tail tip."""
    if s < 0.075:  # rounded snout into the widest part of the head
        return 3.0 + 3.0 * math.sin(s / 0.075 * math.pi / 2)  # blunt, round snout
    if s < 0.165:  # back of the head narrowing into the neck
        return 6.0 - 1.6 * math.sin((s - 0.075) / 0.09 * math.pi / 2)
    if s < 0.56:  # torso swells a little between the legs
        return 4.4 + 1.1 * math.sin((s - 0.165) / 0.395 * math.pi)
    return max(0.35, 4.4 * (1 - (s - 0.56) / 0.44) ** 1.3)  # tail


def outline(pts):
    L = [0.0]
    for i in range(1, len(pts)):
        L.append(L[-1] + math.dist(pts[i - 1], pts[i]))
    total = L[-1]
    left, right, normals = [], [], []
    for i, (x, y) in enumerate(pts):
        j0, j1 = max(0, i - 1), min(len(pts) - 1, i + 1)
        tx, ty = pts[j1][0] - pts[j0][0], pts[j1][1] - pts[j0][1]
        m = math.hypot(tx, ty) or 1
        nx, ny = -ty / m, tx / m
        w = width(L[i] / total)
        left.append((x + nx * w, y + ny * w))
        right.append((x - nx * w, y - ny * w))
        normals.append((nx, ny, L[i] / total))
    return left, right, normals, L, total


def at(pts, L, total, s):
    target = s * total
    for i in range(1, len(L)):
        if L[i] >= target:
            return i
    return len(pts) - 1


def mark(body="#ebe7de", spot="#f5b82e", legs=True, spots=True, size=100):
    pts = centreline()
    left, right, normals, L, total = outline(pts)
    poly = left + right[::-1]
    d = "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in poly) + " Z"
    sx, sy = pts[0]
    parts = [
        f'<path d="{d}" fill="{body}"/>',
        f'<circle cx="{sx:.2f}" cy="{sy:.2f}" r="3.0" fill="{body}"/>',
    ]
    if legs:
        # (position along body, outer length, inner length): inner limbs stay short so
        # they never reach across the S into the other curve.
        for s, outer, inner in ((0.215, 10.5, 8.0), (0.585, 10.5, 6.5)):
            i = at(pts, L, total, s)
            x, y = pts[i]
            nx, ny, _ = normals[i]
            j = min(len(pts) - 1, i + 3)
            tx, ty = pts[j][0] - x, pts[j][1] - y
            m = math.hypot(tx, ty) or 1
            tx, ty = tx / m, ty / m
            front = s < 0.4
            for sign in (1, -1):
                ln = outer if sign == 1 else inner
                # Front limbs reach forward then plant; hind limbs sweep back.
                sweep = -2.6 if front else 2.8
                knee = (
                    x + sign * nx * ln * 0.62 + tx * sweep * 0.4,
                    y + sign * ny * ln * 0.62 + ty * sweep * 0.4,
                )
                foot = (
                    knee[0] + sign * nx * ln * 0.36 + tx * sweep,
                    knee[1] + sign * ny * ln * 0.36 + ty * sweep,
                )
                parts.append(
                    f'<path d="M{x:.2f} {y:.2f} L{knee[0]:.2f} {knee[1]:.2f} '
                    f'L{foot[0]:.2f} {foot[1]:.2f}" fill="none" stroke="{body}" '
                    f'stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>'
                )
                parts.append(f'<circle cx="{foot[0]:.2f}" cy="{foot[1]:.2f}" r="2" fill="{body}"/>')
    if spots:
        for s, r in ((0.085, 1.8), (0.30, 2.2), (0.44, 2.0), (0.66, 1.7), (0.77, 1.2)):
            i = at(pts, L, total, s)
            x, y = pts[i]
            nx, ny, _ = normals[i]
            off = 1.1 if s in (0.30, 0.66) else -1.0
            parts.append(
                f'<circle cx="{x + nx * off:.2f}" cy="{y + ny * off:.2f}" r="{r}" fill="{spot}"/>'
            )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="4 2 92 96" '
        f'width="{size}" height="{size}">' + "".join(parts) + "</svg>"
    )


if __name__ == "__main__":
    import pathlib

    here = pathlib.Path(__file__).parent
    (here / "salamander-mark.svg").write_text(mark())
    (here / "salamander-mark-mono.svg").write_text(mark(body="currentColor", spot="#0d0b0a"))
    (here / "salamander-mark-gold.svg").write_text(mark(body="#f5b82e", spot="#0d0b0a"))
    print("wrote marks")
