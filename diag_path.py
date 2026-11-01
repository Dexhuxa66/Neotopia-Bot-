#!/usr/bin/env python3
"""Offline path diagnostics using the last x-labeled map dump in stay_world.log."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stay_world as S

base = os.path.dirname(os.path.abspath(__file__))
lines = open(f"{base}/stay_world.log", errors="replace").read().splitlines()

start_idx = None
for i in range(len(lines) - 1, -1, -1):
    if "map x=" in lines[i]:
        start_idx = i
        break
assert start_idx is not None, "no labeled map dump"
m = re.search(r"map x=(\d+)\.\.(\d+) y=(\d+)\.\.(\d+)", lines[start_idx])
x0, x1, y0, y1 = map(int, m.groups())
print("using map", m.group(0))
BLOCKS = {}
for line in lines[start_idx + 1:]:
    mm = re.match(r"^.*?map y=(\d+) ([-0-9 ]+)$", line)
    if not mm:
        break
    ty = int(mm.group(1))
    vals = [int(v) for v in mm.group(2).split()]
    for j, v in enumerate(vals):
        if v:
            BLOCKS[ty * S.WORLD_W + (x0 + j)] = v
S.BLOCKS.clear()
S.BLOCKS.update(BLOCKS)
print("loaded", len(BLOCKS), "solid tiles, x%d..%d y%d..%d" % (x0, x1, y0, y1))

def dump(src, direction, kind):
    x, y = S.stand_pos(*src)
    dest, states = S.edge_steps(x, y, direction, kind)
    print(f"edge {src} dir={direction:+d} {kind}: dest={dest} ticks={len(states)} "
          f"stand={S.can_stand(*dest)} end=({states[-1][0]:.1f},{states[-1][1]:.1f})"
          if states else f"edge {src} dir={direction:+d} {kind}: dest={dest} (no states)")

for src in [(79, 27), (78, 27), (77, 27), (80, 28), (84, 29), (85, 29), (75, 26), (76, 27)]:
    print(f"--- {src} can_stand={S.can_stand(*src)}")
    for d in (-1, 0, 1):
        dump(src, d, "drop")
    for d in (-1, 1):
        dump(src, d, "hop")

for src, kind, d in [((79, 27), "drop", -1), ((79, 27), "hop", -1), ((78, 27), "drop", -1)]:
    x, y = S.stand_pos(*src)
    dest, states = S.edge_steps(x, y, d, kind)
    print(f"\npath {src} {kind} dir={d} dest={dest}")
    for i, st in enumerate(states):
        if i % 3 == 0 or i == len(states) - 1:
            print("  t=%3d x=%7.2f y=%7.2f vx=%5.2f vy=%6.2f air=%s" % (i, st[0], st[1], st[2], st[3], st[4]))

for goal in [(76, 35), (70, 35), (63, 38), (65, 38), (68, 37), (70, 42)]:
    p = S.find_path((79, 27), goal, max_nodes=20000)
    print(f"find_path (79,27)->{goal}:", "None" if p is None else
          "; ".join(f"{k}{d:+d}->{dst}" for d, k, dst in p))

print("\n=== deeper chain ===")
for src in [(77, 29), (78, 29), (79, 29), (78, 33), (77, 33), (77, 34), (76, 35), (75, 35), (70, 35), (64, 35), (63, 35)]:
    print(f"--- {src} can_stand={S.can_stand(*src)} floor={S.solid_tile(src[0], src[1]+1)}")
    for d in (-1, 0, 1):
        dump(src, d, "drop")
    for d in (-1, 1):
        dump(src, d, "hop")

print("\n=== full execution check from (88,28) ===")
p = S.find_path((88, 28), (70, 42))
print("path:", "None" if p is None else "; ".join(f"{k}{d:+d}->{dst}" for d, k, dst in p))
if p:
    sx, sy = S.stand_pos(88, 28)
    sim = S.Sim(sx, sy)
    ok = True
    for d, k, dst in p:
        if k == "jump":
            _, states = S.jump_aim_steps(sim.x, sim.y, S.stand_pos(*dst)[0] + 5)
        else:
            _, states = S.edge_steps(sim.x, sim.y, d, k)
        for st in states:
            sim.x, sim.y, sim.vx, sim.vy = st[0], st[1], st[2], st[3]
        actual = S.tile_at(sim.x, sim.y)
        print(f"  {k}{d:+d} -> planned {dst} actual {actual} at ({sim.x:.1f},{sim.y:.1f})",
              "OK" if actual == dst else "MISMATCH")
        ok = ok and actual == dst
    print("all edges land as planned:", ok)
