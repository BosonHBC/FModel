#!/usr/bin/env python3
"""Map Extractor & UE Replicator GUI Tool

Parses FModel-exported UE map JSON files and extracts:
1. StaticMeshComponent: type, transform, mesh asset
2. ISM/HISM: instance transforms, mesh asset
3. Light components: type, transform, light parameters

Provides a tkinter GUI for browsing extracted data and interacting
with UE5 via Python Remote Execution to check/place actors in a level.

Usage: python map_extractor.py
"""

import os, sys, json, re, tempfile, threading, traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

try:
    from ue_remote import UERemoteExec
    HAS_UE_REMOTE = True
except ImportError:
    HAS_UE_REMOTE = False
    UERemoteExec = None

# ======================== Constants ========================

LIGHT_ACTOR_MAP = {
    'PointLightComponent': 'PointLight',
    'SpotLightComponent': 'SpotLight',
    'DirectionalLightComponent': 'DirectionalLight',
    'RectLightComponent': 'RectLight',
    'SkyLightComponent': 'SkyLight',
}

ISM_TYPES = {'InstancedStaticMeshComponent', 'FoliageInstancedStaticMeshComponent'}
HISM_TYPES = {'HierarchicalInstancedStaticMeshComponent'}
LIGHT_TYPES = set(LIGHT_ACTOR_MAP.keys())
SM_TYPES = {'StaticMeshComponent'}

DEFAULT_SRC_PREFIX = '/Game/'
DEFAULT_UE_PREFIX = '/Game/Developers/bosonhuang/SlasherAsset/'
DEFAULT_EXPORTS_ROOT = r'I:\FModelOutput\Exports\SLASHER\Content'
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'map_extractor_config.json')

# ======================== Utilities ========================

def norm_path(p):
    if not p:
        return p
    ls, ld = p.rfind('/'), p.rfind('.')
    return p[:ld] if ld > ls else p


def extract_asset_path(ref):
    """Extract UE asset path from {ObjectName, ObjectPath} dict."""
    if not isinstance(ref, dict):
        return None
    op = ref.get('ObjectPath', '')
    if op:
        return norm_path(op)
    on = ref.get('ObjectName', '')
    m = re.match(r"\w+'([^']+)'", on)
    return m.group(1) if m else None


def extract_actor_name(obj_name):
    """Extract actor instance name from ObjectName like
    "BP_xxx_C'MAP_Ruins_Cellar:PersistentLevel.BP_xxx_C_1'"."""
    if not obj_name:
        return ''
    m = re.search(r"\.([^.:']+)'?$", obj_name)
    return m.group(1) if m else obj_name


def is_mesh_asset_ref(ref):
    """Check if StaticMesh ref points to a mesh asset (not a component)."""
    if not isinstance(ref, dict):
        return False
    on = ref.get('ObjectName', '')
    return bool(on) and "StaticMesh'" in on and "Component" not in on


def map_mesh_path(path, src_prefix, ue_prefix):
    """Apply source->UE path prefix mapping."""
    if not path:
        return path
    if src_prefix and ue_prefix and path.startswith(src_prefix):
        return ue_prefix + path[len(src_prefix):]
    return path


# ======================== BP Template Resolver ========================

class BPTemplateResolver:
    """Resolves BP template references to find mesh assets and component data
    from locally exported BP JSON files."""

    def __init__(self, exports_root=DEFAULT_EXPORTS_ROOT, log=print):
        self.exports_root = exports_root
        self.log = log
        self._bp_cache = {}  # object_path -> parsed bp data

    def _ue_path_to_local(self, ue_path):
        """Convert UE object path like /Game/SLASHER/.../BP_X.3 to local file path."""
        pkg_path = ue_path.rsplit('.', 1)[0]  # strip .N suffix
        if pkg_path.startswith('/Game/'):
            rel = pkg_path[len('/Game/'):]
        else:
            return None
        return os.path.join(self.exports_root, rel.replace('/', '\\') + '.json')

    def _load_bp(self, template_path):
        """Load and cache a BP JSON file by template ObjectPath."""
        if template_path in self._bp_cache:
            return self._bp_cache[template_path]
        local = self._ue_path_to_local(template_path)
        if not local or not os.path.exists(local):
            self._bp_cache[template_path] = None
            return None
        try:
            with open(local, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            self._bp_cache[template_path] = data
            return data
        except Exception as e:
            self.log(f"  Warning: failed to load BP JSON {local}: {e}")
            self._bp_cache[template_path] = None
            return None

    def _extract_component_name(self, template_obj_name):
        """Extract component variable name from Template.ObjectName.
        e.g. 'InstancedStaticMeshComponent'BP_C:InstancedStaticMesh_GEN_VARIABLE''
             -> 'InstancedStaticMesh_GEN_VARIABLE'
        """
        if ':' in template_obj_name:
            part = template_obj_name.split(':', 1)[1]
            return part.rstrip("'")
        return template_obj_name

    def resolve_ism_template(self, entry):
        """Try to resolve mesh asset and instance data from BP template.
        Returns (mesh_asset, mesh_source, instances, bp_found) tuple."""
        template = entry.get('Template', {})
        if not isinstance(template, dict):
            return None, 'bp_template', [], False
        tpl_path = template.get('ObjectPath', '')
        tpl_name = template.get('ObjectName', '')
        if not tpl_path:
            return None, 'bp_template', [], False
        bp_data = self._load_bp(tpl_path)
        if bp_data is None:
            return None, 'bp_template', [], False
        comp_name = self._extract_component_name(tpl_name)
        # Find ISM component in BP with matching Name
        ism_types_all = ISM_TYPES | HISM_TYPES
        for bp_entry in bp_data:
            if not isinstance(bp_entry, dict):
                continue
            if bp_entry.get('Type', '') not in ism_types_all:
                continue
            if bp_entry.get('Name', '') != comp_name:
                continue
            props = bp_entry.get('Properties', {})
            mesh = extract_asset_path(props.get('StaticMesh', {})) if props.get('StaticMesh') else None
            raw_instances = bp_entry.get('PerInstanceSMData', [])
            instances = []
            for inst in raw_instances:
                td = inst.get('TransformData', {})
                r, t, s = td.get('Rotation', {}), td.get('Translation', {}), td.get('Scale3D', {})
                instances.append({
                    'rotation': {'x': r.get('X', 0), 'y': r.get('Y', 0), 'z': r.get('Z', 0), 'w': r.get('W', 1)},
                    'translation': {'x': t.get('X', 0), 'y': t.get('Y', 0), 'z': t.get('Z', 0)},
                    'scale3d': {'x': s.get('X', 1), 'y': s.get('Y', 1), 'z': s.get('Z', 1)},
                })
            source = 'bp_template' if instances else 'bp_template'
            return mesh, source, instances, True
        return None, 'bp_template', [], True

    def resolve_sm_template(self, entry):
        """Try to resolve StaticMesh asset from BP template for SM components."""
        template = entry.get('Template', {})
        if not isinstance(template, dict):
            return None, False
        tpl_path = template.get('ObjectPath', '')
        tpl_name = template.get('ObjectName', '')
        if not tpl_path:
            return None, False
        bp_data = self._load_bp(tpl_path)
        if bp_data is None:
            return None, False
        comp_name = self._extract_component_name(tpl_name)
        for bp_entry in bp_data:
            if not isinstance(bp_entry, dict):
                continue
            if bp_entry.get('Type', '') not in SM_TYPES:
                continue
            if bp_entry.get('Name', '') != comp_name:
                continue
            props = bp_entry.get('Properties', {})
            mesh = extract_asset_path(props.get('StaticMesh', {})) if props.get('StaticMesh') else None
            return mesh, True
        return None, True

    def get_missing_bp_paths(self):
        """Return set of template paths that couldn't be found locally."""
        missing = set()
        for tpl_path, data in self._bp_cache.items():
            if data is None:
                missing.add(tpl_path)
        return missing


# ======================== Map Parser ========================

class MapParser:
    """Parses FModel map JSON and extracts relevant component data."""

    def __init__(self, json_path):
        self.json_path = json_path
        self.entries = []
        self.actor_labels = {}
        self.static_meshes = []
        self.instanced_meshes = []
        self.lights = []
        self.unique_meshes = set()
        self.map_name = os.path.basename(json_path).replace('.json', '')
        self.bp_resolver = None
        self.missing_bp_paths = set()

    def parse(self, log=print):
        log(f'Loading JSON: {self.json_path}')
        with open(self.json_path, 'r', encoding='utf-8-sig') as f:
            self.entries = json.load(f)
        total = len(self.entries)
        log(f'Loaded {total} entries')

        # First pass: index actor labels
        log('Indexing actor labels...')
        for entry in self.entries:
            if not isinstance(entry, dict):
                continue
            props = entry.get('Properties', {})
            if 'ActorLabel' in props:
                name = entry.get('Name', '')
                outer_name = entry.get('Outer', {}).get('ObjectName', '')
                actor_name = extract_actor_name(outer_name) or name
                self.actor_labels[actor_name] = props['ActorLabel']
                self.actor_labels[name] = props['ActorLabel']

        # Second pass: extract components
        log('Extracting components...')
        self.bp_resolver = BPTemplateResolver(log=log)
        for i, entry in enumerate(self.entries):
            if not isinstance(entry, dict):
                continue
            t = entry.get('Type', '')
            if t in SM_TYPES:
                self._extract_sm(entry)
            elif t in ISM_TYPES or t in HISM_TYPES:
                self._extract_ism(entry)
            elif t in LIGHT_TYPES:
                self._extract_light(entry)
            if i % 200000 == 0 and i > 0:
                log(f'  ...{i}/{total}')

        # Collect missing BP template paths
        self.missing_bp_paths = self.bp_resolver.get_missing_bp_paths()
        if self.missing_bp_paths:
            log(f'  Warning: {len(self.missing_bp_paths)} BP templates not found locally:')
            for p in sorted(self.missing_bp_paths):
                log(f'    {p}')

        total_instances = sum(x.get('instance_count', 0) for x in self.instanced_meshes)
        # Count unique meshes across all components
        all_meshes = set()
        for item in self.static_meshes:
            if item.get('mesh_asset'):
                all_meshes.add(item['mesh_asset'])
        for item in self.instanced_meshes:
            if item.get('mesh_asset'):
                all_meshes.add(item['mesh_asset'])
        self.unique_meshes = all_meshes
        summary = {
            'map_name': self.map_name,
            'total_entries': total,
            'static_meshes': len(self.static_meshes),
            'instanced_meshes': len(self.instanced_meshes),
            'total_instances': total_instances,
            'lights': len(self.lights),
            'unique_meshes': len(all_meshes),
        }
        log(f'Done: {summary}')
        return summary

    def _get_label(self, outer_name, name):
        actor = extract_actor_name(outer_name)
        return self.actor_labels.get(actor, self.actor_labels.get(name, actor or name))

    def _extract_transform(self, props):
        loc = props.get('RelativeLocation', {})
        rot = props.get('RelativeRotation', {})
        scale = props.get('RelativeScale3D', {})
        t = {}
        if loc:
            t['location'] = {'x': loc.get('X', 0), 'y': loc.get('Y', 0), 'z': loc.get('Z', 0)}
        if rot:
            t['rotation'] = {'pitch': rot.get('Pitch', 0), 'yaw': rot.get('Yaw', 0), 'roll': rot.get('Roll', 0)}
        if scale:
            t['scale'] = {'x': scale.get('X', 1), 'y': scale.get('Y', 1), 'z': scale.get('Z', 1)}
        return t or None

    def _extract_sm(self, entry):
        props = entry.get('Properties', {})
        outer_name = entry.get('Outer', {}).get('ObjectName', '')
        mesh = None
        sm_ref = props.get('StaticMesh', {})
        if is_mesh_asset_ref(sm_ref):
            mesh = extract_asset_path(sm_ref)
        template = entry.get('Template', {})
        tpl_path = norm_path(template.get('ObjectPath', '')) if isinstance(template, dict) else None
        bp_found = False
        if mesh is None and self.bp_resolver:
            mesh, bp_found = self.bp_resolver.resolve_sm_template(entry)
        self.static_meshes.append({
            'name': entry.get('Name', ''),
            'type': 'StaticMeshComponent',
            'outer_actor': extract_actor_name(outer_name),
            'actor_label': self._get_label(outer_name, entry.get('Name', '')),
            'mesh_asset': mesh,
            'transform': self._extract_transform(props),
            'mobility': props.get('Mobility', ''),
            'template_path': tpl_path,
            'bp_resolved': bp_found if mesh else (True if mesh else False),
        })

    def _extract_ism(self, entry):
        props = entry.get('Properties', {})
        outer_name = entry.get('Outer', {}).get('ObjectName', '')
        etype = entry.get('Type', '')
        mesh = extract_asset_path(props.get('StaticMesh', {})) if props.get('StaticMesh') else None
        # PerInstanceSMData is at entry top-level (sibling of Properties), not inside Properties
        raw_instances = entry.get('PerInstanceSMData', [])
        instances = []
        for inst in raw_instances:
            td = inst.get('TransformData', {})
            r, t, s = td.get('Rotation', {}), td.get('Translation', {}), td.get('Scale3D', {})
            instances.append({
                'rotation': {'x': r.get('X', 0), 'y': r.get('Y', 0), 'z': r.get('Z', 0), 'w': r.get('W', 1)},
                'translation': {'x': t.get('X', 0), 'y': t.get('Y', 0), 'z': t.get('Z', 0)},
                'scale3d': {'x': s.get('X', 1), 'y': s.get('Y', 1), 'z': s.get('Z', 1)},
            })
        template = entry.get('Template', {})
        tpl_path = norm_path(template.get('ObjectPath', '')) if isinstance(template, dict) else None
        # If no mesh or instances from level data, try BP template
        bp_found = False
        from_bp = False
        if mesh is None and self.bp_resolver:
            mesh, mesh_source, bp_instances, bp_found = self.bp_resolver.resolve_ism_template(entry)
            from_bp = True
            if not instances and bp_instances:
                instances = bp_instances
        # Determine mesh source
        if from_bp and bp_found:
            mesh_source = 'bp_template' if len(instances) > 0 else 'bp_template'
        elif mesh is not None and len(instances) > 0:
            mesh_source = 'level_data'
        elif mesh is not None and len(instances) == 0:
            mesh_source = 'level_data_mesh_only'
        elif bp_found:
            mesh_source = 'bp_template'
        else:
            mesh_source = 'bp_template_missing'
        self.instanced_meshes.append({
            'name': entry.get('Name', ''),
            'type': etype,
            'outer_actor': extract_actor_name(outer_name),
            'actor_label': self._get_label(outer_name, entry.get('Name', '')),
            'mesh_asset': mesh,
            'mesh_source': mesh_source,
            'instance_count': len(instances),
            'instances': instances,
            'component_transform': self._extract_transform(props),
            'template_path': tpl_path,
            'bp_resolved': bp_found,
        })

    def _extract_light(self, entry):
        props = entry.get('Properties', {})
        outer_name = entry.get('Outer', {}).get('ObjectName', '')
        etype = entry.get('Type', '')
        params = {}
        for k in ['Intensity', 'AttenuationRadius', 'LightFalloffExponent', 'SourceRadius',
                   'SourceLength', 'bUseInverseSquaredFalloff', 'IndirectLightingIntensity',
                   'VolumetricScatteringIntensity', 'OuterConeAngle', 'InnerConeAngle',
                   'IntensityUnits', 'MaxDrawDistance', 'MaxDistanceFadeRange', 'CastShadows',
                   'bUseTemperature', 'Temperature', 'Mobility', 'bCastStaticShadow',
                   'bCastDynamicShadow', 'bAffectGlobalIllumination', 'InverseExposureBlend',
                   'NanitePixelProgrammableDistance']:
            if k in props:
                v = props[k]
                if isinstance(v, dict) and 'R' in v:
                    params['light_color'] = {'r': v.get('R', 255), 'g': v.get('G', 255),
                                             'b': v.get('B', 255), 'a': v.get('A', 255)}
                else:
                    params[k] = v
        nits = entry.get('IntensityNits')
        if nits is not None:
            params['intensity_nits'] = nits
        self.lights.append({
            'name': entry.get('Name', ''),
            'type': etype,
            'outer_actor': extract_actor_name(outer_name),
            'actor_label': self._get_label(outer_name, entry.get('Name', '')),
            'transform': self._extract_transform(props),
            'params': params,
        })

    def to_simplified_json(self):
        # Collect unique meshes with usage stats
        mesh_usage = {}
        for item in self.static_meshes:
            m = item.get('mesh_asset')
            if m:
                mesh_usage.setdefault(m, {'count': 0, 'sources': set()})
                mesh_usage[m]['count'] += 1
                mesh_usage[m]['sources'].add('SM')
        for item in self.instanced_meshes:
            m = item.get('mesh_asset')
            if m:
                mesh_usage.setdefault(m, {'count': 0, 'sources': set()})
                mesh_usage[m]['count'] += 1
                mesh_usage[m]['sources'].add('ISM')
        unique_mesh_list = []
        for mesh, info in sorted(mesh_usage.items()):
            unique_mesh_list.append({
                'mesh_asset': mesh,
                'component_count': info['count'],
                'sources': sorted(info['sources']),
            })
        # BP resolution stats
        bp_resolved = sum(1 for x in self.instanced_meshes if x.get('bp_resolved'))
        bp_missing = sum(1 for x in self.instanced_meshes if x.get('mesh_source') == 'bp_template_missing')
        return {
            'map_name': self.map_name,
            'source_file': self.json_path,
            'summary': {
                'static_mesh_components': len(self.static_meshes),
                'instanced_mesh_components': len(self.instanced_meshes),
                'total_instances': sum(x.get('instance_count', 0) for x in self.instanced_meshes),
                'light_components': len(self.lights),
                'unique_meshes': len(unique_mesh_list),
                'bp_templates_resolved': bp_resolved,
                'bp_templates_missing': bp_missing,
            },
            'missing_bp_paths': sorted(self.missing_bp_paths),
            'unique_meshes': unique_mesh_list,
            'static_mesh_components': self.static_meshes,
            'instanced_mesh_components': self.instanced_meshes,
            'light_components': self.lights,
        }


# ======================== UE Placer ========================

class UEPlacer:
    """Handles UE interaction for checking and placing actors."""

    def __init__(self, host='239.0.0.1', port=6766):
        self.client = None
        self.host = host
        self.port = port
        self.connected = False

    def connect(self):
        if not HAS_UE_REMOTE:
            return False, "ue_remote.py not found in " + SCRIPT_DIR
        # Clean up any previous client (sockets/threads may still be alive)
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
        # Create a DebugLog to capture diagnostic info
        from ue_remote import DebugLog
        self._dbg = DebugLog()
        self.client = UERemoteExec(self.host, self.port, dbg=self._dbg)
        ok = self.client.connect(timeout=15)
        self.connected = ok
        if ok:
            return True, "Connected to UE"
        else:
            # Surface debug log so user can see what went wrong
            lines = self._dbg.get_lines()
            detail = "\n".join(lines[-20:]) if lines else "(no debug output)"
            return False, f"Connection failed\n--- Debug Log ---\n{detail}"

    def disconnect(self):
        if self.client:
            self.client.disconnect()
        self.connected = False

    def _run(self, cmd, timeout=120):
        if not self.connected:
            return "", "Not connected"
        r = self.client.run_command(cmd, mode='exec', timeout=timeout)
        out = r.get('output', '')
        if isinstance(out, list):
            out = ''.join(str(x.get('output', '')) if isinstance(x, dict) else str(x) for x in out)
        result = ''
        for line in str(out).splitlines():
            if line.strip().startswith('RESULT::'):
                result = line.strip()[8:]
        err = r.get('error', '')
        return result, (err if err else '')

    def get_level_actors(self):
        """Get all actors in current level. Returns list of {label, class, x, y, z}."""
        cmd = (
            'import unreal, json\n'
            'actors = unreal.EditorLevelLibrary.get_all_level_actors()\n'
            'data = []\n'
            'for a in actors:\n'
            '    loc = a.get_actor_location()\n'
            '    data.append({"label": a.get_actor_label(), "class": a.get_class().get_name(),'
            ' "x": float(loc.x), "y": float(loc.y), "z": float(loc.z)})\n'
            'print("RESULT::" + json.dumps(data))\n'
        )
        result, err = self._run(cmd, timeout=30)
        try:
            return json.loads(result) if result else []
        except Exception:
            return []

    def load_level(self, level_asset_path):
        """Load a specific level in UE editor by asset path.
        Returns True if successful, False otherwise."""
        cmd = (
            'import unreal, traceback\n'
            'try:\n'
            f'    asset = unreal.load_asset(r"{level_asset_path}")\n'
            '    if not asset:\n'
            '        print("RESULT::False")\n'
            '    else:\n'
            f'        unreal.EditorLevelLibrary.load_level(r"{level_asset_path}")\n'
            '        print("RESULT::True")\n'
            'except Exception as e:\n'
            '    traceback.print_exc()\n'
            '    print("RESULT::False")\n'
        )
        result, err = self._run(cmd, timeout=60)
        return result.strip() == 'True'

    def check_mesh_assets(self, mesh_paths):
        """Batch check which mesh asset paths exist in UE.
        Returns dict {mesh_path: True/False}."""
        if not mesh_paths:
            return {}
        # Write mesh list to temp file to avoid command string size limits
        tmp = os.path.join(tempfile.gettempdir(), 'ue_check_meshes.json')
        with open(tmp, 'w') as f:
            json.dump(sorted(mesh_paths), f)
        tmp_fwd = tmp.replace('\\', '/')
        cmd = (
            'import unreal, json\n'
            f'with open(r"{tmp_fwd}") as f:\n'
            '    meshes = json.load(f)\n'
            'results = {}\n'
            'for path in meshes:\n'
            '    obj_path = path + "." + path.rsplit("/", 1)[-1]\n'
            '    asset = unreal.load_asset(obj_path)\n'
            '    results[path] = asset is not None\n'
            'print("RESULT::" + json.dumps(results))\n'
        )
        result, err = self._run(cmd, timeout=120)
        try:
            return json.loads(result) if result else {}
        except Exception:
            return {}

    def place_static_mesh(self, item, src_prefix='', ue_prefix=''):
        mesh = item.get('mesh_asset') or ''
        if not mesh:
            return "ERROR: No mesh asset (may be from BP template)"
        mesh = map_mesh_path(mesh, src_prefix, ue_prefix)
        mesh_obj = mesh + '.' + os.path.basename(mesh)
        tf = item.get('transform') or {}
        loc = tf.get('location', {})
        rot = tf.get('rotation', {})
        scale = tf.get('scale', {})
        label = (item.get('actor_label') or item.get('name', '')).replace('"', '')
        lx, ly, lz = loc.get('x', 0), loc.get('y', 0), loc.get('z', 0)
        rp, ry, rr = rot.get('pitch', 0), rot.get('yaw', 0), rot.get('roll', 0)
        sx, sy, sz = scale.get('x', 1), scale.get('y', 1), scale.get('z', 1)
        cmd = (
            'import unreal, traceback\n'
            'def _run():\n'
            f'    mesh = unreal.load_asset(r"{mesh_obj}")\n'
            f'    if not mesh: return "ERROR: Mesh not found: {mesh}"\n'
            f'    loc = unreal.Vector({lx}, {ly}, {lz})\n'
            f'    rot = unreal.Rotator({rp}, {ry}, {rr})\n'
            '    actor = unreal.EditorLevelLibrary.spawn_actor_from_class('
            'unreal.StaticMeshActor, loc, rot)\n'
            '    if not actor: return "ERROR: Spawn failed"\n'
            '    smc = actor.get_editor_property("static_mesh_component")\n'
            '    if smc: smc.set_static_mesh(mesh)\n'
            f'    actor.set_actor_scale3d(unreal.Vector({sx}, {sy}, {sz}))\n'
            f'    actor.set_actor_label("{label}")\n'
            f'    return "OK: {label}"\n'
            'try:\n'
            '    print("RESULT::" + str(_run()))\n'
            'except Exception as e:\n'
            '    traceback.print_exc()\n'
            '    print("RESULT::ERROR: " + str(e))\n'
        )
        result, err = self._run(cmd, timeout=30)
        return result or err or "No response"

    def place_ism(self, item, src_prefix='', ue_prefix=''):
        mesh = item.get('mesh_asset') or ''
        if not mesh:
            return "ERROR: No mesh asset (may be from BP template)"
        mesh = map_mesh_path(mesh, src_prefix, ue_prefix)
        mesh_obj = mesh + '.' + os.path.basename(mesh)
        instances = item.get('instances', [])
        label = (item.get('actor_label') or item.get('name', '')).replace('"', '')
        is_hism = 'Hierarchical' in item.get('type', '')
        comp_cls = 'HierarchicalInstancedStaticMeshComponent' if is_hism else 'InstancedStaticMeshComponent'
        # Write instances to temp file to avoid command string size limits
        tmp = os.path.join(tempfile.gettempdir(), 'ue_ism_instances.json')
        with open(tmp, 'w') as f:
            json.dump(instances, f)
        tmp_fwd = tmp.replace('\\', '/')
        cmd = (
            'import unreal, json, traceback\n'
            'def _run():\n'
            f'    mesh = unreal.load_asset(r"{mesh_obj}")\n'
            f'    if not mesh: return "ERROR: Mesh not found: {mesh}"\n'
            '    actor = unreal.EditorLevelLibrary.spawn_actor_from_class('
            'unreal.Actor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n'
            '    if not actor: return "ERROR: Spawn failed"\n'
            f'    comp = unreal.new_object(actor, unreal.{comp_cls}, "ISM")\n'
            '    comp.set_static_mesh(mesh)\n'
            '    actor.set_editor_property("root_component", comp)\n'
            '    comp.register_component()\n'
            f'    with open(r"{tmp_fwd}") as f:\n'
            '        instances = json.load(f)\n'
            '    for inst in instances:\n'
            '        t = inst["translation"]; r = inst["rotation"]; s = inst["scale3d"]\n'
            '        xform = unreal.Transform(\n'
            '            unreal.Vector(t["x"], t["y"], t["z"]),\n'
            '            unreal.Quat(r["x"], r["y"], r["z"], r["w"]),\n'
            '            unreal.Vector(s["x"], s["y"], s["z"]))\n'
            '        comp.add_instance(xform)\n'
            f'    actor.set_actor_label("{label}")\n'
            '    return "OK: {label} (%d instances)" % len(instances)\n'
            'try:\n'
            '    print("RESULT::" + str(_run()))\n'
            'except Exception as e:\n'
            '    traceback.print_exc()\n'
            '    print("RESULT::ERROR: " + str(e))\n'
        )
        result, err = self._run(cmd, timeout=180)
        return result or err or "No response"

    def place_light(self, item):
        ltype = item.get('type', 'PointLightComponent')
        actor_cls = LIGHT_ACTOR_MAP.get(ltype, 'PointLight')
        tf = item.get('transform') or {}
        loc = tf.get('location', {})
        rot = tf.get('rotation', {})
        params = item.get('params', {})
        label = (item.get('actor_label') or item.get('name', '')).replace('"', '')
        intensity = params.get('Intensity', 0)
        color = params.get('light_color', {})
        r, g, b = color.get('r', 255), color.get('g', 255), color.get('b', 255)
        atten = params.get('AttenuationRadius', 0)
        lx, ly, lz = loc.get('x', 0), loc.get('y', 0), loc.get('z', 0)
        rp, ry, rr = rot.get('pitch', 0), rot.get('yaw', 0), rot.get('roll', 0)
        atten_line = f'    comp.set_editor_property("attenuation_radius", {atten})\n' if atten else ''
        cmd = (
            'import unreal, traceback\n'
            'def _run():\n'
            f'    loc = unreal.Vector({lx}, {ly}, {lz})\n'
            f'    rot = unreal.Rotator({rp}, {ry}, {rr})\n'
            '    actor = unreal.EditorLevelLibrary.spawn_actor_from_class('
            f'unreal.{actor_cls}, loc, rot)\n'
            '    if not actor: return "ERROR: Spawn failed"\n'
            '    comp = actor.get_component_by_class(unreal.LightComponent)\n'
            '    if comp:\n'
            f'        comp.set_editor_property("intensity", {intensity})\n'
            f'        comp.set_editor_property("light_color", '
            f'unreal.LinearColor({r}/255.0, {g}/255.0, {b}/255.0, 1.0))\n'
            f'{atten_line}'
            f'    actor.set_actor_label("{label}")\n'
            f'    return "OK: {label}"\n'
            'try:\n'
            '    print("RESULT::" + str(_run()))\n'
            'except Exception as e:\n'
            '    traceback.print_exc()\n'
            '    print("RESULT::ERROR: " + str(e))\n'
        )
        result, err = self._run(cmd, timeout=30)
        return result or err or "No response"


# ======================== GUI ========================

class MapExtractorGUI:
    """Main GUI application."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Map Extractor & UE Replicator")
        self.parser = None
        self.placer = UEPlacer()
        self.ue_actors = {}
        self.match_status = {}
        self.mesh_in_ue = {}
        self._last_json_dir = r"I:\FModelOutput\Exports\SLASHER\Content\SLASHER\Maps"
        self._config = self._load_config()
        self.root.geometry(self._config.get('window_geometry', '1300x850'))
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # ---- Toolbar ----
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill='x', padx=5, pady=3)
        ttk.Button(toolbar, text="Load Map JSON", command=self._load_json).pack(side='left')
        ttk.Button(toolbar, text="Export Simplified JSON", command=self._export_json).pack(side='left', padx=4)
        ttk.Separator(toolbar, orient='vertical').pack(side='left', fill='y', padx=4)
        ttk.Button(toolbar, text="Connect UE", command=self._connect_ue).pack(side='left')
        ttk.Button(toolbar, text="Disconnect", command=self._disconnect_ue).pack(side='left', padx=4)
        ttk.Separator(toolbar, orient='vertical').pack(side='left', fill='y', padx=4)
        ttk.Button(toolbar, text="Check Assets", command=self._check_assets).pack(side='left')
        ttk.Button(toolbar, text="Check Map", command=self._check_map).pack(side='left', padx=4)
        ttk.Button(toolbar, text="Place Selected", command=self._place_selected).pack(side='left')
        ttk.Button(toolbar, text="Place All Unmatched", command=self._place_all).pack(side='left')

        # ---- Path mapping frame ----
        pm = ttk.LabelFrame(self.root, text="Mesh Path Mapping (Source -> UE)")
        pm.pack(fill='x', padx=5, pady=2)
        ttk.Label(pm, text="Source Prefix:").grid(row=0, column=0, sticky='w', padx=3)
        self.src_prefix_var = tk.StringVar(value=self._config.get('src_prefix', DEFAULT_SRC_PREFIX))
        ttk.Entry(pm, textvariable=self.src_prefix_var, width=45).grid(row=0, column=1, padx=3)
        ttk.Label(pm, text="UE Prefix:").grid(row=0, column=2, sticky='w', padx=3)
        self.ue_prefix_var = tk.StringVar(value=self._config.get('ue_prefix', DEFAULT_UE_PREFIX))
        ttk.Entry(pm, textvariable=self.ue_prefix_var, width=45).grid(row=0, column=3, padx=3)
        ttk.Label(pm, text="(e.g. /Game/ -> /Game/Developers/.../SlasherAsset/)").grid(row=0, column=4, padx=5)

        # ---- UE Level path ----
        lp = ttk.LabelFrame(self.root, text="UE Level Path (for map comparison)")
        lp.pack(fill='x', padx=5, pady=2)
        ttk.Label(lp, text="Level Asset:").grid(row=0, column=0, sticky='w', padx=3)
        self.ue_level_var = tk.StringVar(value=self._config.get('ue_level_path', "/Game/SLASHER/Maps/Ruins/RuinsCellar/MAP_Ruins_Cellar"))
        ttk.Entry(lp, textvariable=self.ue_level_var, width=60).grid(row=0, column=1, padx=3)
        ttk.Label(lp, text="(UE asset path of the level to compare against, e.g. /Game/.../MAP_Ruins_Cellar)").grid(row=0, column=2, padx=5)

        # ---- Search bar ----
        sf = ttk.Frame(self.root)
        sf.pack(fill='x', padx=5, pady=2)
        ttk.Label(sf, text="Filter:").pack(side='left')
        self.search_var = tk.StringVar()
        self.search_var.trace('w', self._on_search)
        ttk.Entry(sf, textvariable=self.search_var, width=40).pack(side='left', padx=5)
        self.count_var = tk.StringVar(value="")
        ttk.Label(sf, textvariable=self.count_var).pack(side='left', padx=10)

        # ---- Main area ----
        main = ttk.PanedWindow(self.root, orient='horizontal')
        main.pack(fill='both', expand=True, padx=5, pady=3)

        # Treeview
        tree_frame = ttk.Frame(main)
        tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical')
        self.tree = ttk.Treeview(tree_frame, columns=('asset', 'map', 'type', 'mesh'),
                                 show='tree headings', yscrollcommand=tree_scroll.set)
        tree_scroll.config(command=self.tree.yview)
        self.tree.heading('#0', text='Item')
        self.tree.heading('asset', text='Asset')
        self.tree.heading('map', text='Map')
        self.tree.heading('type', text='Type')
        self.tree.heading('mesh', text='Mesh / Details')
        self.tree.column('#0', width=260)
        self.tree.column('asset', width=55, anchor='center')
        self.tree.column('map', width=55, anchor='center')
        self.tree.column('type', width=180)
        self.tree.column('mesh', width=300)
        self.tree.pack(side='left', fill='both', expand=True)
        tree_scroll.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<Double-1>', lambda e: self._place_selected())
        main.add(tree_frame, weight=3)

        # Detail panel
        detail_frame = ttk.Frame(main)
        self.detail_text = tk.Text(detail_frame, wrap='word', state='disabled',
                                   font=('Consolas', 10))
        detail_scroll = ttk.Scrollbar(detail_frame, orient='vertical',
                                      command=self.detail_text.yview)
        self.detail_text.config(yscrollcommand=detail_scroll.set)
        self.detail_text.pack(side='left', fill='both', expand=True)
        detail_scroll.pack(side='right', fill='y')
        main.add(detail_frame, weight=2)

        # ---- Log ----
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill='x', padx=5, pady=3)
        self.log_text = tk.Text(log_frame, height=7, wrap='word', state='disabled',
                                font=('Consolas', 9))
        self.log_text.pack(fill='x')

        # ---- Status bar ----
        self.status_var = tk.StringVar(value="Ready. Load a map JSON to begin.")
        ttk.Label(self.root, textvariable=self.status_var, relief='sunken').pack(
            fill='x', side='bottom')

    def _load_config(self):
        """Load saved settings from config file."""
        defaults = {
            'src_prefix': DEFAULT_SRC_PREFIX,
            'ue_prefix': DEFAULT_UE_PREFIX,
            'ue_level_path': "/Game/SLASHER/Maps/Ruins/RuinsCellar/MAP_Ruins_Cellar",
            'last_json_dir': r"I:\FModelOutput\Exports\SLASHER\Content\SLASHER\Maps",
            'window_geometry': "1300x850",
        }
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                defaults.update(saved)
        except Exception:
            pass
        return defaults

    def _save_config(self):
        """Persist current user-editable values to config file."""
        cfg = {
            'src_prefix': self.src_prefix_var.get(),
            'ue_prefix': self.ue_prefix_var.get(),
            'ue_level_path': self.ue_level_var.get(),
            'last_json_dir': self._config.get('last_json_dir', ''),
            'window_geometry': self.root.geometry(),
        }
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _on_close(self):
        """Save config and destroy window on close."""
        self._save_config()
        self.root.destroy()

    def _log(self, msg):
        self.log_text.config(state='normal')
        self.log_text.insert('end', str(msg) + '\n')
        self.log_text.see('end')
        self.log_text.config(state='disabled')

    def _set_status(self, msg):
        self.status_var.set(msg)

    # ---- File operations ----

    def _load_json(self):
        path = filedialog.askopenfilename(
            title="Select Map JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialdir=self._config.get('last_json_dir', self._last_json_dir)
        )
        if not path:
            return
        self._config['last_json_dir'] = os.path.dirname(path)
        self._save_config()
        self._set_status("Parsing (this may take a moment for large files)...")
        self._log(f"Loading: {path}")

        def work():
            try:
                self.parser = MapParser(path)
                self.parser.parse(log=self._log)
                self.root.after(0, self._populate_tree)
            except Exception as e:
                err = traceback.format_exc()
                self.root.after(0, lambda: self._log(f"ERROR:\n{err}"))
                self.root.after(0, lambda: self._set_status("Error loading file"))

        threading.Thread(target=work, daemon=True).start()

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        p = self.parser
        # Static Meshes
        sm_node = self.tree.insert('', 'end',
                                    text=f"Static Mesh Components ({len(p.static_meshes)})",
                                    values=('', '', '', ''))
        for i, item in enumerate(p.static_meshes):
            label = item.get('actor_label') or item.get('name', f'item_{i}')
            mesh = item.get('mesh_asset') or '(from template)'
            self.tree.insert(sm_node, 'end', text=label,
                             values=('?', '?', item['type'], mesh),
                             tags=('sm', str(i)))
        # ISM/HISM
        ism_node = self.tree.insert('', 'end',
                                    text=f"ISM/HISM Components ({len(p.instanced_meshes)})",
                                    values=('', '', '', ''))
        total_inst = 0
        bp_resolved_count = 0
        bp_missing_count = 0
        for i, item in enumerate(p.instanced_meshes):
            label = item.get('actor_label') or item.get('name', f'item_{i}')
            mesh = item.get('mesh_asset') or '(no mesh)'
            n = item.get('instance_count', 0)
            total_inst += n
            src = item.get('mesh_source', '')
            bp_tag = ''
            if src == 'bp_template':
                bp_tag = ' [BP]'
                bp_resolved_count += 1
            elif src == 'bp_template_missing':
                bp_tag = ' [BP-MISSING]'
                bp_missing_count += 1
            elif src == 'level_data_mesh_only':
                bp_tag = ' [mesh-only]'
            self.tree.insert(ism_node, 'end', text=f"{label} [{n} inst]{bp_tag}",
                             values=('?', '?', item['type'], mesh),
                             tags=('ism', str(i)))
        # Lights
        light_node = self.tree.insert('', 'end',
                                      text=f"Light Components ({len(p.lights)})",
                                      values=('', '', '', ''))
        for i, item in enumerate(p.lights):
            label = item.get('actor_label') or item.get('name', f'item_{i}')
            ptype = item.get('type', '')
            self.tree.insert(light_node, 'end', text=label,
                             values=('', '?', ptype, ''),
                             tags=('light', str(i)))
        self.count_var.set(
            f"SM: {len(p.static_meshes)} | ISM/HISM: {len(p.instanced_meshes)} "
            f"({total_inst} instances) | Lights: {len(p.lights)} | "
            f"Unique Meshes: {len(p.unique_meshes)} | "
            f"BP resolved: {bp_resolved_count}, missing: {bp_missing_count}")
        self._set_status("Loaded. Expand categories to browse items.")

    def _on_search(self, *args):
        q = self.search_var.get().lower().strip()
        if not q:
            return
        # Expand all and search
        for node in self.tree.get_children():
            for child in self.tree.get_children(node):
                text = self.tree.item(child, 'text').lower()
                if q in text:
                    self.tree.see(child)
                    self.tree.selection_set(child)
                    return

    def _on_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        item_info = self.tree.item(sel[0])
        tags = item_info.get('tags', [])
        if not tags:
            return
        cat, idx = tags[0], int(tags[1])
        data = None
        if cat == 'sm':
            data = self.parser.static_meshes[idx]
        elif cat == 'ism':
            data = self.parser.instanced_meshes[idx]
            # Truncate instances display for large arrays
            if len(data.get('instances', [])) > 50:
                disp = dict(data)
                disp['instances'] = data['instances'][:50]
                disp['_note'] = f"(showing 50 of {len(data['instances'])} instances)"
                data = disp
        elif cat == 'light':
            data = self.parser.lights[idx]
        if not data:
            return
        self.detail_text.config(state='normal')
        self.detail_text.delete('1.0', 'end')
        self.detail_text.insert('1.0', json.dumps(data, indent=2, ensure_ascii=False))
        self.detail_text.config(state='disabled')

    def _export_json(self):
        if not self.parser:
            messagebox.showwarning("Warning", "No map loaded")
            return
        path = filedialog.asksaveasfilename(
            title="Export Simplified JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialdir=os.path.dirname(self.parser.json_path)
        )
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.parser.to_simplified_json(), f, indent=2, ensure_ascii=False)
            self._log(f"Exported to: {path}")
            self._set_status(f"Exported: {path}")
        except Exception as e:
            self._log(f"Export error: {e}")

    # ---- UE operations ----

    def _connect_ue(self):
        if not HAS_UE_REMOTE:
            messagebox.showerror("Error", "ue_remote.py not found.\n"
                                          "Ensure it's in the same directory as this script.")
            return
        self._set_status("Connecting to UE...")

        def work():
            ok, msg = self.placer.connect()
            self.root.after(0, lambda: self._log(msg))
            self.root.after(0, lambda: self._set_status(
                "Connected to UE" if ok else "Connection failed"))
        threading.Thread(target=work, daemon=True).start()

    def _disconnect_ue(self):
        self.placer.disconnect()
        self._log("Disconnected from UE")
        self._set_status("Disconnected")

    def _check_assets(self):
        if not self.placer.connected:
            messagebox.showwarning("Warning", "Not connected to UE")
            return
        if not self.parser:
            messagebox.showwarning("Warning", "No map loaded")
            return
        self._set_status("Checking mesh assets in UE...")

        def work():
            src_pfx = self.src_prefix_var.get().strip()
            ue_pfx = self.ue_prefix_var.get().strip()
            all_meshes = set()
            for item in self.parser.static_meshes:
                m = item.get('mesh_asset')
                if m:
                    all_meshes.add(map_mesh_path(m, src_pfx, ue_pfx))
            for item in self.parser.instanced_meshes:
                m = item.get('mesh_asset')
                if m:
                    all_meshes.add(map_mesh_path(m, src_pfx, ue_pfx))
            if all_meshes:
                self._log(f"Checking {len(all_meshes)} unique mesh assets in UE...")
                self.mesh_in_ue = self.placer.check_mesh_assets(all_meshes)
                exists_count = sum(1 for v in self.mesh_in_ue.values() if v)
                self._log(f"Asset check: {exists_count} exist, {len(all_meshes) - exists_count} missing")
            else:
                self.mesh_in_ue = {}
                self._log("No mesh assets to check.")
                exists_count = 0
            total = len(all_meshes)
            self.root.after(0, self._update_tree_status)
            self.root.after(0, lambda: self._set_status(
                f"Asset: {exists_count}/{total} exist in UE"))
        threading.Thread(target=work, daemon=True).start()

    def _check_map(self):
        if not self.placer.connected:
            messagebox.showwarning("Warning", "Not connected to UE")
            return
        if not self.parser:
            messagebox.showwarning("Warning", "No map loaded")
            return
        level_path = self.ue_level_var.get().strip()
        if not level_path:
            messagebox.showwarning("Warning", "Please specify a UE Level Path for comparison")
            return
        self._set_status("Loading UE level and checking actors...")

        def work():
            self._log(f"Loading UE level: {level_path} ...")
            load_ok = self.placer.load_level(level_path)
            if not load_ok:
                self._log("WARNING: Failed to load level. Map check skipped. "
                          "Asset check (if any) remains valid.")
                self.root.after(0, lambda: self._set_status("Level load failed — map check skipped"))
                return

            self._log("Querying UE level actors...")
            actors = self.placer.get_level_actors()
            self.ue_actors = {a['label']: a for a in actors}
            self._log(f"Found {len(actors)} actors in UE level")

            p = self.parser
            matched = 0
            unmatched = 0
            for cat, items in [('sm', p.static_meshes),
                               ('ism', p.instanced_meshes),
                               ('light', p.lights)]:
                for i, item in enumerate(items):
                    label = item.get('actor_label', '')
                    if label and label in self.ue_actors:
                        self.match_status[(cat, i)] = 'matched'
                        matched += 1
                    else:
                        self.match_status[(cat, i)] = 'unmatched'
                        unmatched += 1
            self.root.after(0, self._update_tree_status)
            self.root.after(0, lambda: self._log(
                f"Map check complete: {matched} matched in level, {unmatched} unmatched"))
            # Preserve existing asset stats in status bar if available
            asset_str = ""
            if self.mesh_in_ue:
                ex = sum(1 for v in self.mesh_in_ue.values() if v)
                asset_str = f"Asset: {ex}/{len(self.mesh_in_ue)} exist | "
            self.root.after(0, lambda: self._set_status(
                f"{asset_str}Map: {matched} matched, {unmatched} unmatched"))
        threading.Thread(target=work, daemon=True).start()

    def _update_tree_status(self):
        src_pfx = self.src_prefix_var.get().strip()
        ue_pfx = self.ue_prefix_var.get().strip()
        for node in self.tree.get_children():
            for child in self.tree.get_children(node):
                tags = self.tree.item(child, 'tags')
                if not tags:
                    continue
                cat, idx = tags[0], int(tags[1])
                # Update 'map' column: whether this actor/component exists in the UE level
                status = self.match_status.get((cat, idx), 'unknown')
                map_symbol = '\u2713' if status == 'matched' else ('\u2717' if status == 'unmatched' else '?')
                vals = list(self.tree.item(child, 'values'))
                vals[1] = map_symbol  # 'map' is index 1
                # Update 'asset' column: whether the mesh asset exists in UE content browser
                item = None
                if cat == 'sm':
                    item = self.parser.static_meshes[idx]
                elif cat == 'ism':
                    item = self.parser.instanced_meshes[idx]
                if item and item.get('mesh_asset'):
                    mapped = map_mesh_path(item['mesh_asset'], src_pfx, ue_pfx)
                    exists = self.mesh_in_ue.get(mapped, None)
                    vals[0] = '\u2713' if exists else ('\u2717' if exists is False else '?')
                else:
                    vals[0] = '-'  # no mesh asset (from template)
                self.tree.item(child, values=tuple(vals))

    def _place_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        item_info = self.tree.item(sel[0])
        tags = item_info.get('tags', [])
        if not tags:
            return
        cat, idx = tags[0], int(tags[1])
        if not self.placer.connected:
            messagebox.showwarning("Warning", "Not connected to UE")
            return
        self._set_status(f"Placing {cat}:{idx}...")

        def work():
            self._place_item(cat, idx)
        threading.Thread(target=work, daemon=True).start()

    def _place_item(self, cat, idx):
        src_pfx = self.src_prefix_var.get().strip()
        ue_pfx = self.ue_prefix_var.get().strip()
        if cat == 'sm':
            item = self.parser.static_meshes[idx]
            result = self.placer.place_static_mesh(item, src_pfx, ue_pfx)
        elif cat == 'ism':
            item = self.parser.instanced_meshes[idx]
            result = self.placer.place_ism(item, src_pfx, ue_pfx)
        elif cat == 'light':
            item = self.parser.lights[idx]
            result = self.placer.place_light(item)
        else:
            return
        self.root.after(0, lambda: self._log(f"Place [{cat}:{idx}]: {result}"))
        self.root.after(0, lambda: self._set_status(f"Placed: {result[:80]}"))

    def _place_all(self):
        if not self.placer.connected:
            messagebox.showwarning("Warning", "Not connected to UE")
            return
        if not self.match_status:
            messagebox.showinfo("Info", "Run 'Check All in UE' first to identify unmatched items.")
            return
        unmatched = [(cat, idx) for (cat, idx), s in self.match_status.items() if s == 'unmatched']
        if not unmatched:
            messagebox.showinfo("Info", "All items are matched!")
            return
        self._set_status(f"Placing {len(unmatched)} unmatched items...")

        def work():
            count = 0
            for cat, idx in unmatched:
                self._place_item(cat, idx)
                count += 1
                self.root.after(0, lambda c=count: self._set_status(f"Placing {c}/{len(unmatched)}..."))
            self.root.after(0, lambda: self._log(f"Done: placed {count} items"))
            self.root.after(0, lambda: self._set_status(f"Placed {count} items"))
        threading.Thread(target=work, daemon=True).start()

    def run(self):
        self.root.mainloop()


# ======================== Entry Point ========================

def main():
    app = MapExtractorGUI()
    app.run()


if __name__ == '__main__':
    main()
