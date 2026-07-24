#!/usr/bin/env python3
"""UE StaticMesh Showcase Layout Script

Scans all StaticMesh assets under a specified UE content path,
groups them by directory (same directory = one group),
calculates AABB-based grid layout (roughly rectangular, not a strip),
and spawns them in the current level with Outliner folders.

Requirements:
  - UE5 editor running with Python Remote Execution enabled
  - ue_remote.py in the same directory

Usage:
    python mesh_showcase.py
    python mesh_showcase.py --base-path /Game/Path/To/Assets --spacing 100
    python mesh_showcase.py --dry-run          # preview layout only
    python mesh_showcase.py --clear-existing   # delete old showcase actors first
"""

import sys
import os
import math
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ue_remote import UERemoteExec

DEFAULT_BASE_PATH = "/Game/Developers/bosonhuang/SlasherAsset/SLASHER"
DEFAULT_SPACING = 100.0  # UE units (cm)
DEFAULT_TARGET_SIZE = 500.0  # target max AABB dimension (cm) for normalize mode


class MeshShowcase:
    """Places all StaticMesh assets from a content path into the current level."""

    def __init__(self, base_path=DEFAULT_BASE_PATH, spacing=DEFAULT_SPACING,
                 folder_prefix="Showcase", dry_run=False, clear_existing=False,
                 layout_mode="original", target_size=DEFAULT_TARGET_SIZE,
                 variant_z_spacing=100.0):
        self.base_path = base_path.rstrip('/')
        self.spacing = spacing
        self.folder_prefix = folder_prefix
        self.dry_run = dry_run
        self.clear_existing = clear_existing
        self.layout_mode = layout_mode  # "original" or "normalize"
        self.target_size = target_size  # used only in normalize mode
        self.variant_z_spacing = variant_z_spacing  # Z offset between variant meshes (_A, _B, _C)
        self.client = None

    # ---- lifecycle ----

    def log(self, msg):
        print(msg)

    def connect(self):
        self.client = UERemoteExec()
        if not self.client.connect(timeout=15):
            self.log("ERROR: Cannot connect to UE.")
            self.log("  Ensure UE is running with Python Remote Execution enabled.")
            return False
        self.log("OK: Connected to UE")
        return True

    def disconnect(self):
        if self.client:
            self.client.disconnect()

    def run(self):
        if not self.connect():
            return False
        try:
            # Step 1
            groups = self._scan_and_group_meshes()
            if not groups:
                self.log("No StaticMesh assets found under " + self.base_path)
                return False

            self.log("\nFound %d group(s):" % len(groups))
            for g in groups:
                sz = g['aabb_size']
                self.log("  %-40s %2d mesh(es)  AABB=(%.0f x %.0f x %.0f)" %
                         (g['folder_name'], len(g['meshes']), sz[0], sz[1], sz[2]))

            self.log("\n  Layout mode: %s" % self.layout_mode)

            # Step 2
            layout = self._calculate_layout(groups)

            # Step 3
            if self.dry_run:
                self.log("\n[DRY RUN] Skipping placement.")
            else:
                if self.clear_existing:
                    self._clear_existing()
                self._place_meshes(groups, layout)

            self.log("\n=== Done! ===")
            return True
        except Exception as e:
            self.log("ERROR: " + str(e))
            import traceback
            traceback.print_exc()
            return False
        finally:
            self.disconnect()

    # ---- Step 1: scan & group ----

    def _scan_and_group_meshes(self):
        self.log("\n--- Step 1: Scanning StaticMesh assets ---")
        self.log("  Base path: " + self.base_path)

        cmd = r'''
import unreal, json, re

base_path = "%(bp)s"
all_assets = unreal.EditorAssetLibrary.list_assets(base_path, recursive=True, include_folder=False)

# Phase 1: collect meshes by directory
dir_meshes = {}
for ap in all_assets:
    asset = unreal.EditorAssetLibrary.load_asset(ap)
    if asset is None or not isinstance(asset, unreal.StaticMesh):
        continue

    pkg = ap.rsplit('.', 1)[0]
    parts = pkg.rsplit('/', 1)
    folder = parts[0] if len(parts) > 1 else pkg
    name   = parts[1] if len(parts) > 1 else ""

    # bounds
    try:
        b = asset.get_bounds()
        ox, oy, oz = b.origin.x, b.origin.y, b.origin.z
        ex, ey, ez = b.box_extent.x, b.box_extent.y, b.box_extent.z
    except Exception:
        try:
            b = asset.get_editor_property("bounds")
            ox, oy, oz = b.origin.x, b.origin.y, b.origin.z
            ex, ey, ez = b.box_extent.x, b.box_extent.y, b.box_extent.z
        except Exception:
            ox = oy = oz = 0.0
            ex = ey = ez = 50.0

    info = {"path": ap, "name": name,
            "origin": [ox, oy, oz], "extent": [ex, ey, ez]}
    dir_meshes.setdefault(folder, []).append(info)

# Phase 2: within each directory, sub-group by prefix
# Strip "SM_" prefix, then group by everything before the last "_"
def get_prefix(mesh_name):
    base = mesh_name
    if base.startswith("SM_"):
        base = base[3:]
    idx = base.rfind("_")
    if idx > 0:
        return base[:idx]
    return base

# Detect variant suffix: _A, _B, _C, _01, _02, etc.
# Both letter and numeric suffixes follow the same logic.
# Exception: if the name contains "piece" (case-insensitive), always return None
# because those are parts of the same mesh, not variants.
def get_variant_suffix(mesh_name):
    base = mesh_name
    if base.startswith("SM_"):
        base = base[3:]
    # "piece" in name => same mesh parts, not variants
    if "piece" in base.lower():
        return None
    # Match patterns like _A, _B, _C (single uppercase letter)
    m = re.search(r'_([A-Z])$', base)
    if m:
        return m.group(1)
    # Match patterns like _01, _02, _1, _2 (preserve leading zeros for correct sort)
    m = re.search(r'_(\d+)$', base)
    if m:
        return m.group(1)
    return None

result = []
for folder, meshes in dir_meshes.items():
    sub_groups = {}
    for m in meshes:
        prefix = get_prefix(m["name"])
        m["variant_suffix"] = get_variant_suffix(m["name"])
        sub_groups.setdefault(prefix, []).append(m)

    rel = folder
    if folder.startswith(base_path):
        rel = folder[len(base_path):].lstrip('/')
    if not rel:
        rel = "Root"

    multi = len(sub_groups) > 1
    for prefix, sub_meshes in sub_groups.items():
        if multi:
            fname = rel + "/" + prefix
        else:
            fname = rel

        mins = [min(m["origin"][i] - m["extent"][i] for m in sub_meshes) for i in range(3)]
        maxs = [max(m["origin"][i] + m["extent"][i] for m in sub_meshes) for i in range(3)]
        result.append({
            "folder": folder,
            "folder_name": fname,
            "meshes": sub_meshes,
            "aabb_min": mins,
            "aabb_max": maxs,
            "aabb_size": [maxs[i] - mins[i] for i in range(3)],
        })

result.sort(key=lambda g: g["folder_name"])
print("RESULT::" + json.dumps(result))
''' % {'bp': self.base_path}

        r = self._run_cmd(cmd, timeout=300)
        if not r['success']:
            self.log("  ERROR: " + r['error'][:500])
            return []
        try:
            return json.loads(r['result_text'])
        except Exception as e:
            self.log("  ERROR parsing: " + str(e))
            return []

    # ---- Step 2: layout ----

    def _calculate_layout(self, groups):
        self.log("\n--- Step 2: Calculating layout ---")

        # In normalize mode: compute a uniform scale factor per group so the
        # largest AABB dimension equals target_size, preserving XYZ ratio.
        if self.layout_mode == "normalize":
            for g in groups:
                max_dim = max(g['aabb_size'])
                if max_dim > 1e-4:
                    g['scale'] = self.target_size / max_dim
                else:
                    g['scale'] = 1.0
                scaled_size = [s * g['scale'] for s in g['aabb_size']]
                g['scaled_aabb_size'] = scaled_size
                g['scaled_aabb_min'] = [m * g['scale'] for m in g['aabb_min']]
                self.log("  %-40s scale=%.3f  scaled_AABB=(%.0f x %.0f x %.0f)" %
                         (g['folder_name'], g['scale'],
                          scaled_size[0], scaled_size[1], scaled_size[2]))
        else:
            for g in groups:
                g['scale'] = 1.0
                g['scaled_aabb_size'] = list(g['aabb_size'])
                g['scaled_aabb_min'] = list(g['aabb_min'])

        n = len(groups)
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)

        # per-column max width, per-row max depth (using scaled sizes)
        col_w = [0.0] * cols
        row_d = [0.0] * rows
        for i, g in enumerate(groups):
            c, row = i % cols, i // cols
            col_w[c] = max(col_w[c], g['scaled_aabb_size'][0])
            row_d[row] = max(row_d[row], g['scaled_aabb_size'][1])

        # cumulative offsets
        ox = [0.0]
        for w in col_w[:-1]:
            ox.append(ox[-1] + w + self.spacing)
        oy = [0.0]
        for d in row_d[:-1]:
            oy.append(oy[-1] + d + self.spacing)

        positions = []
        for i, g in enumerate(groups):
            c, row = i % cols, i // cols
            # offset so group AABB corner sits at cell origin, Z bottom = 0
            sx = ox[c] - g['scaled_aabb_min'][0]
            sy = oy[row] - g['scaled_aabb_min'][1]
            sz = -g['scaled_aabb_min'][2]
            positions.append((sx, sy, sz))

        total_w = (ox[-1] + col_w[-1]) if cols else 0
        total_d = (oy[-1] + row_d[-1]) if rows else 0

        self.log("  Groups=%d  Cols=%d  Rows=%d" % (n, cols, rows))
        self.log("  Total footprint: %.0f x %.0f" % (total_w, total_d))
        return {'cols': cols, 'rows': rows, 'positions': positions}

    # ---- Step 3: placement ----

    def _clear_existing(self):
        """Delete all actors whose folder path starts with our prefix."""
        self.log("\n  Clearing existing showcase actors...")
        cmd = r'''
import unreal
prefix = "%(prefix)s"
sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
all_actors = sub.get_all_level_actors()
deleted = 0
for a in all_actors:
    try:
        fp = a.get_folder_path()
        if fp and str(fp).startswith(prefix):
            sub.destroy_actor(a)
            deleted += 1
    except Exception:
        pass
print("RESULT::" + str(deleted))
''' % {'prefix': self.folder_prefix}
        r = self._run_cmd(cmd, timeout=120)
        if r['success']:
            self.log("  Deleted %s actor(s)" % r['result_text'])

    def _place_meshes(self, groups, layout):
        self.log("\n--- Step 3: Placing meshes ---")

        z_spacing = self.variant_z_spacing

        for i, group in enumerate(groups):
            pos = layout['positions'][i]
            fname = group['folder_name']
            outliner = self.folder_prefix + "/" + fname
            gscale = group.get('scale', 1.0)

            # Build per-mesh placement info: each mesh may get a Z offset
            # if it has a different variant suffix than others in the group
            mesh_list = group['meshes']

            # Detect if this group has multiple distinct variant suffixes
            suffixes = [m.get('variant_suffix') for m in mesh_list]
            has_variants = len(set(suffixes)) > 1

            mesh_placements = []  # (mesh_path, z_offset)
            if has_variants:
                # Assign an incrementing Z offset to each distinct variant
                # Sort: numeric suffixes by value, letter suffixes alphabetically
                def variant_sort_key(s):
                    if s is not None and s.isdigit():
                        return (1, int(s), s)
                    return (0, 0, s or "")
                sorted_unique = sorted(set(s for s in suffixes if s is not None),
                                       key=variant_sort_key)
                suffix_to_z = {}
                for idx2, sfx in enumerate(sorted_unique):
                    suffix_to_z[sfx] = idx2 * z_spacing

                for m in mesh_list:
                    z_off = suffix_to_z.get(m.get('variant_suffix'), 0.0)
                    mesh_placements.append((m['path'], z_off))
            else:
                for m in mesh_list:
                    mesh_placements.append((m['path'], 0.0))

            self.log("  [%d/%d] %s  ->  (%.0f, %.0f, %.0f)  scale=%.3f  [%d mesh(es), variants=%s]" %
                     (i + 1, len(groups), fname, pos[0], pos[1], pos[2], gscale,
                      len(mesh_list), "yes" if has_variants else "no"))

            cmd = r'''
import unreal

mesh_placements = %(mp)s
base_loc = unreal.Vector(%(sx).2f, %(sy).2f, %(sz).2f)
rot = unreal.Rotator(0, 0, 0)
scl = unreal.Vector(%(sc).4f, %(sc).4f, %(sc).4f)
fp   = "%(fp)s"

count = 0
for mp, z_off in mesh_placements:
    asset = unreal.EditorAssetLibrary.load_asset(mp)
    if not asset:
        continue
    loc = unreal.Vector(base_loc.x, base_loc.y, base_loc.z + z_off)
    actor = unreal.EditorLevelLibrary.spawn_actor_from_object(asset, loc, rot)
    if not actor:
        continue
    actor.set_actor_scale3d(scl)
    try:
        actor.set_folder_path(fp)
    except Exception:
        try:
            actor.set_editor_property("folder_path", unreal.DirectoryPath(fp))
        except Exception:
            pass
    count += 1

print("RESULT::" + str(count))
''' % {
                'mp': json.dumps(mesh_placements),
                'sx': pos[0], 'sy': pos[1], 'sz': pos[2],
                'sc': gscale,
                'fp': outliner,
            }

            r = self._run_cmd(cmd, timeout=180)
            if r['success']:
                self.log("    -> spawned %s" % r['result_text'])
            else:
                self.log("    -> ERROR: " + r['error'][:300])

    # ---- util ----

    def _run_cmd(self, cmd, mode='exec', timeout=120):
        """Execute in UE; parse RESULT:: marker from stdout."""
        r = self.client.run_command(cmd, mode=mode, timeout=timeout)

        out = r.get('output', '')
        if isinstance(out, list):
            out_text = ''.join(
                str(it.get('output', '')) if isinstance(it, dict) else str(it)
                for it in out
            )
        else:
            out_text = str(out)

        result_text = ''
        for line in out_text.splitlines():
            line = line.strip()
            if line.startswith('RESULT::'):
                result_text = line[len('RESULT::'):]

        err = r.get('error', '') or ''
        if err:
            self.log("  (UE warning: %s)" % err[:200])

        return {
            'success': r.get('success', False) and not err,
            'result_text': result_text,
            'output_text': out_text,
            'error': err,
        }


def main():
    parser = argparse.ArgumentParser(description="UE StaticMesh Showcase Layout")
    parser.add_argument('--base-path', default=DEFAULT_BASE_PATH,
                        help='Content path to scan (default: %(default)s)')
    parser.add_argument('--spacing', type=float, default=DEFAULT_SPACING,
                        help='Gap between groups in cm (default: %(default)s)')
    parser.add_argument('--folder-prefix', default='Showcase',
                        help='Root Outliner folder (default: %(default)s)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview layout without spawning actors')
    parser.add_argument('--clear-existing', action='store_true',
                        help='Delete existing actors under folder-prefix first')
    parser.add_argument('--layout-mode', choices=['original', 'normalize'], default='original',
                        help='Layout mode: original (1:1 scale) or normalize (scale all meshes '
                             'to similar size, default: %(default)s)')
    parser.add_argument('--target-size', type=float, default=DEFAULT_TARGET_SIZE,
                        help='Target max AABB dimension in cm for normalize mode '
                             '(default: %(default)s)')
    parser.add_argument('--variant-z-spacing', type=float, default=100.0,
                        help='Z offset between variant meshes (_A, _B, _C) within a group '
                             'in cm (default: %(default)s)')
    args = parser.parse_args()

    app = MeshShowcase(
        base_path=args.base_path,
        spacing=args.spacing,
        folder_prefix=args.folder_prefix,
        dry_run=args.dry_run,
        clear_existing=args.clear_existing,
        layout_mode=args.layout_mode,
        target_size=args.target_size,
        variant_z_spacing=args.variant_z_spacing,
    )
    sys.exit(0 if app.run() else 1)


if __name__ == '__main__':
    main()
