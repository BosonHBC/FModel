#!/usr/bin/env python3
"""UE Static Mesh Importer V2 - Data-driven material import pipeline.

5-step pipeline (as specified):
1. Import GLB mesh from Mesh.json
2. Collect materials & textures (trace MI -> Master Material chains)
3. Import all textures (sRGB/compression from Master Material data, not guessing)
4. Create Master Materials if not in engine (texture params match JSON names)
5. Create MaterialInstanceConstants (fill texture values from MI JSON)
6. Assign materials to mesh sections

Old script (mesh_importer.py) is kept as reference. Key pitfalls fixed:
- OLD: Used simplified JSON format -> Parent chain broke -> couldn't find Master Material
  NEW: Always use FModel raw JSON (list with Properties/CachedExpressionData)
- OLD: Created standalone Materials with TextureSample nodes (no parameters)
  NEW: Creates Master Material with TextureSampleParameter2D, then MI overrides them
- OLD: Guessed texture role from filename suffix only
  NEW: Uses Master Material's CachedExpressionData (param_name -> default_texture -> suffix) deterministically
- OLD: Didn't filter junk param names (texture filenames as keys)
  NEW: Only uses param names that exist in Master Material's RuntimeEntries[3]
- OLD: Material assignment failed (material_dest_path not set)
  NEW: Explicitly builds section -> material_dest_path mapping
"""

import os, sys, json, re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from ue_remote import UERemoteExec, test_connection


# ======================== Constants ========================

# Role -> UE texture settings
ROLE_SETTINGS = {
    'diffuse': {
        'srgb': True,
        'compression': 'unreal.TextureCompressionSettings.TC_DEFAULT',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_BASE_COLOR')],
    },
    'normal': {
        'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_NORMALMAP',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_NORMAL')],
    },
    'orm': {
        'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_MASKS',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [
            ('R', 'unreal.MaterialProperty.MP_AMBIENT_OCCLUSION'),
            ('G', 'unreal.MaterialProperty.MP_ROUGHNESS'),
            ('B', 'unreal.MaterialProperty.MP_METALLIC'),
        ],
    },
    'mask': {
        'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_MASKS',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_MASKS',
        'connections': [],  # Mask textures don't connect to standard outputs directly
    },
}


# ======================== Path Utilities ========================

def norm_path(p):
    """Strip trailing .0 or .N suffix from UE object paths."""
    if not p:
        return p
    ls, ld = p.rfind('/'), p.rfind('.')
    return p[:ld] if ld > ls else p


def get_fmodel_roots(fmodel_root):
    """Get all possible root directories for FModel exports.
    FModel layout:
      - JSON  at: I:/FModelOutput/Exports/SLASHER/Content/SLASHER/...
      - GLB/PNG at: I:/FModelOutput/SLASHER/Content/SLASHER/...  (no 'Exports')
    Both share the '.../Content' + UE-relative-path structure, so we return both
    the given root and its variant with the 'Exports' segment removed/added.
    """
    norm = fmodel_root.replace('\\', '/').rstrip('/')
    roots = [norm]
    parts = norm.split('/')
    # Variant with 'Exports' removed
    if 'Exports' in parts:
        no_exports = '/'.join(p for p in parts if p != 'Exports')
        if no_exports not in roots:
            roots.append(no_exports)
    else:
        # Variant with 'Exports' inserted before the first 'SLASHER'/content root
        # Insert 'Exports' right after the drive/base if there's a known content marker
        for i, p in enumerate(parts):
            if p and i > 0:
                with_exports = '/'.join(parts[:i] + ['Exports'] + parts[i:])
                if with_exports not in roots:
                    roots.append(with_exports)
                break
    return roots


def ue_path_to_local(ue_path, fmodel_root, ext):
    """Find a local file for a UE path with given extension."""
    rel = ue_path.replace('/Game/', '', 1) if ue_path.startswith('/Game/') else ue_path
    for root in get_fmodel_roots(fmodel_root):
        local = os.path.join(root, rel + ext)
        if os.path.isfile(local):
            return local
    return None


def ue_path_to_import_dest(ue_path, content_root):
    """Convert /Game/xxx/Asset to destination under content_root.
    For /Engine/ or /Script/ paths, return as-is (engine built-in assets)."""
    if ue_path.startswith('/Engine/') or ue_path.startswith('/Script/'):
        return ue_path
    rel = ue_path.replace('/Game/', '', 1) if ue_path.startswith('/Game/') else ue_path
    return content_root.rstrip('/') + '/' + rel


# ======================== JSON Parsing ========================

def load_json(path):
    """Load JSON with utf-8-sig encoding (FModel exports have BOM)."""
    with open(path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def find_json(ue_path, fmodel_root):
    """Find the JSON file for a UE asset path."""
    return ue_path_to_local(ue_path, fmodel_root, '.json')


def find_glb(ue_path, fmodel_root):
    """Find the GLB file for a UE mesh path."""
    return ue_path_to_local(ue_path, fmodel_root, '.glb')


def find_texture_png(ue_path, fmodel_root):
    """Find the PNG texture file for a UE texture path."""
    return ue_path_to_local(ue_path, fmodel_root, '.png')


def parse_mesh_sections(mesh_json_path):
    """Parse StaticMaterials from a StaticMesh JSON file.
    Returns list of {slot_name, material_path, material_type}.
    """
    data = load_json(mesh_json_path)
    entries = data if isinstance(data, list) else [data]
    sections = []
    for e in entries:
        if not isinstance(e, dict) or e.get('Type') != 'StaticMesh':
            continue
        sms = e.get('Properties', {}).get('StaticMaterials', [])
        for sm in sms:
            mi = sm.get('MaterialInterface', {})
            obj_name = mi.get('ObjectName', '')
            obj_path = mi.get('ObjectPath', '')
            if not obj_path:
                continue
            mat_path = norm_path(obj_path)
            if 'MaterialInstanceConstant' in obj_name:
                mat_type = 'MaterialInstanceConstant'
            elif 'Material' in obj_name:
                mat_type = 'Material'
            else:
                mat_type = 'Material'
            sections.append({
                'slot_name': sm.get('MaterialSlotName', f'Slot_{len(sections)}'),
                'material_path': mat_path,
                'material_type': mat_type,
            })
        break
    return sections


def parse_material_json(ue_path, fmodel_root):
    """Load and parse a material JSON file.
    Returns (json_path, json_data) or (None, None).
    """
    jp = find_json(ue_path, fmodel_root)
    if not jp:
        return None, None
    return jp, load_json(jp)


def get_parent_path(mat_data):
    """Extract Parent ObjectPath from material JSON data."""
    entries = mat_data if isinstance(mat_data, list) else [mat_data]
    for e in entries:
        if not isinstance(e, dict):
            continue
        props = e.get('Properties', {})
        parent = props.get('Parent', {})
        if isinstance(parent, dict):
            obj_path = parent.get('ObjectPath', '')
            if obj_path:
                return norm_path(obj_path)
    return None


def get_texture_params(mat_data):
    """Extract TextureParameterValues from material JSON.
    Returns list of {param_name, texture_path} (texture_path may be None).
    """
    entries = mat_data if isinstance(mat_data, list) else [mat_data]
    result = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        props = e.get('Properties', {})
        tpv = props.get('TextureParameterValues', [])
        for tp in tpv:
            if not isinstance(tp, dict):
                continue
            param_info = tp.get('ParameterInfo', {})
            param_name = param_info.get('Name', '') if isinstance(param_info, dict) else ''
            val = tp.get('ParameterValue')
            tex_path = None
            if isinstance(val, dict):
                obj_path = val.get('ObjectPath', '')
                if obj_path and obj_path.startswith('/Game/'):
                    tex_path = norm_path(obj_path)
            result.append({'param_name': param_name, 'texture_path': tex_path})
        break
    return result


def get_scalar_params(mat_data):
    """Extract ScalarParameterValues from material JSON.
    Returns list of {param_name, value}.
    """
    entries = mat_data if isinstance(mat_data, list) else [mat_data]
    result = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        props = e.get('Properties', {})
        spv = props.get('ScalarParameterValues', [])
        for sp in spv:
            if not isinstance(sp, dict):
                continue
            param_info = sp.get('ParameterInfo', {})
            param_name = param_info.get('Name', '') if isinstance(param_info, dict) else ''
            value = sp.get('ParameterValue', 0.0)
            if param_name:
                result.append({'param_name': param_name, 'value': float(value)})
        break
    return result


def get_material_type(mat_data):
    """Determine if JSON is a Material or MaterialInstanceConstant."""
    entries = mat_data if isinstance(mat_data, list) else [mat_data]
    for e in entries:
        if isinstance(e, dict):
            t = e.get('Type', '')
            if 'MaterialInstanceConstant' in t:
                return 'MaterialInstanceConstant'
            if t == 'Material':
                return 'Material'
    return 'Material'


def get_master_material_tex_params(mat_data):
    """Extract texture parameter names and default textures from Master Material JSON.
    Uses CachedExpressionData.RuntimeEntries[3] (param names) and TextureValues (default textures).
    Returns list of {param_name, default_tex_filename} in index order.
    """
    entries = mat_data if isinstance(mat_data, list) else [mat_data]
    param_names = []
    default_tex_names = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        cached = e.get('CachedExpressionData', {})
        re3 = cached.get('RuntimeEntries[3]', {})
        for p in re3.get('ParameterInfoSet', []):
            param_names.append(p.get('Name', ''))
        for tv in cached.get('TextureValues', []):
            asset_path = tv.get('AssetPathName', '')
            # Extract filename from path like /Game/.../T_Atlass_A_N.T_Atlass_A_N
            basename = os.path.basename(asset_path)
            # Strip .ext suffix
            if '.' in basename:
                basename = basename.split('.')[0]
            default_tex_names.append(basename)
        break
    result = []
    for i, pname in enumerate(param_names):
        default_tex = default_tex_names[i] if i < len(default_tex_names) else ''
        result.append({'param_name': pname, 'default_tex_filename': default_tex})
    return result


# ======================== Texture Role Resolver ========================

def guess_role_from_suffix(filename):
    """Guess texture role from filename suffix. Returns role or None."""
    if not filename:
        return None
    name = filename.upper()
    # Check longer suffixes first to avoid false matches
    suffix_map = [
        ('_OCCLUSIONROUGHNESSMETALLIC', 'orm'),
        ('_ORM', 'orm'),
        ('_NORMAL', 'normal'),
        ('_BASECOLOR', 'diffuse'),
        ('_ALBEDO', 'diffuse'),
        ('_DISPLACEMENT', 'mask'),
        ('_HEIGHT', 'mask'),
        ('_CAVITY', 'mask'),
        ('_MR', 'mask'),
        ('_N', 'normal'),
        ('_M', 'mask'),
        ('_C', 'mask'),
        ('_A', 'diffuse'),
    ]
    for suffix, role in suffix_map:
        if name.endswith(suffix):
            return role
    return None


def guess_role_from_param_name(param_name):
    """Guess texture role from parameter name. Returns role or None."""
    if not param_name:
        return None
    pn = param_name.lower()
    if 'normal' in pn:
        return 'normal'
    if 'orm' in pn or 'roughness' in pn or 'specular' in pn:
        return 'orm'
    if 'mask' in pn or 'cavity' in pn or 'convex' in pn or 'thickness' in pn or 'height' in pn or 'displacement' in pn or 'noise' in pn:
        return 'mask'
    if 'albedo' in pn or 'base' in pn or 'color' in pn or 'sand' in pn or 'tileable' in pn or 'overlay' in pn:
        return 'diffuse'
    return None


def determine_texture_role(param_name, default_tex_filename):
    """Determine texture role deterministically from Master Material data.
    Priority: 1) default texture suffix, 2) param name semantics, 3) diffuse fallback.
    This is NOT guessing - it uses the actual default textures assigned in the Master Material.
    """
    role = guess_role_from_suffix(default_tex_filename)
    if role:
        return role
    role = guess_role_from_param_name(param_name)
    if role:
        return role
    return 'diffuse'


# ======================== Material Chain Resolver ========================

class MaterialChainResolver:
    """Traces material parent chains and collects all dependencies."""

    def __init__(self, fmodel_root):
        self.fmodel_root = fmodel_root
        # master_materials: {ue_path: {json_path, json_data, tex_params (from CachedExpressionData)}}
        self.master_materials = {}
        # material_instances: ordered list of {ue_path, parent_path, json_data, tex_params, scalar_params}
        # Ordered so parents come before children
        self.material_instances = []
        # mi_paths set for quick lookup
        self.mi_paths = set()
        # all_textures: {ue_path: {local_file, role, srgb, compression, param_name, source}}
        self.all_textures = {}
        # sections: [{slot_name, material_path, material_type}]
        self.sections = []

    def resolve(self, mesh_json_path):
        """Main entry: resolve all dependencies for a mesh."""
        print("\n=== Step 2: Collecting materials & textures ===")
        self.sections = parse_mesh_sections(mesh_json_path)
        print(f"  Found {len(self.sections)} material sections in mesh JSON")

        for s in self.sections:
            print(f"  Section [{s['slot_name']}]: {s['material_path']} ({s['material_type']})")
            self._resolve_material(s['material_path'])

        print(f"\n  Master Materials: {len(self.master_materials)}")
        for mp in self.master_materials:
            print(f"    - {mp}")
        print(f"  Material Instances: {len(self.material_instances)}")
        for mi in self.material_instances:
            print(f"    - {mi['ue_path']} (parent: {mi['parent_path']})")
        print(f"  Textures: {len(self.all_textures)}")

        return {
            'sections': self.sections,
            'master_materials': self.master_materials,
            'material_instances': self.material_instances,
            'all_textures': self.all_textures,
        }

    def _resolve_material(self, mat_path):
        """Resolve a material: trace parent chain, collect textures."""
        if mat_path in self.mi_paths or mat_path in self.master_materials:
            return  # Already resolved

        jp, jd = parse_material_json(mat_path, self.fmodel_root)
        if not jp:
            print(f"    WARNING: JSON not found for {mat_path}")
            return

        mat_type = get_material_type(jd)
        parent_path = get_parent_path(jd)

        if mat_type == 'MaterialInstanceConstant':
            self._resolve_mi(mat_path, jp, jd, parent_path)
        else:
            # It's a Material (root Master Material or standalone)
            self._resolve_master_material(mat_path, jp, jd)

    def _resolve_mi(self, mi_path, json_path, json_data, parent_path):
        """Resolve an MI: register it, trace parent, collect textures."""
        if mi_path in self.mi_paths:
            return
        self.mi_paths.add(mi_path)

        # Resolve parent first (so parent appears before child in order)
        if parent_path:
            self._resolve_material(parent_path)
        else:
            print(f"    WARNING: MI {mi_path} has no parent, treating as standalone")

        # Parse this MI's texture and scalar params
        tex_params = get_texture_params(json_data)
        scalar_params = get_scalar_params(json_data)

        # Find the root Master Material for role determination
        master_path = self._find_root_master(mi_path)
        master_tex_param_map = {}
        if master_path and master_path in self.master_materials:
            for tp in self.master_materials[master_path]['tex_params']:
                master_tex_param_map[tp['param_name']] = tp['default_tex_filename']

        # Collect textures from this MI
        for tp in tex_params:
            param_name = tp['param_name']
            tex_path = tp['texture_path']
            if not tex_path:
                continue  # MI doesn't override this param (inherits from parent)
            if tex_path.startswith('/Script/') or tex_path.startswith('/Engine/'):
                continue

            # Determine role from Master Material data
            default_tex_name = master_tex_param_map.get(param_name, '')
            role = determine_texture_role(param_name, default_tex_name)
            settings = ROLE_SETTINGS[role]

            if tex_path not in self.all_textures:
                local_file = find_texture_png(tex_path, self.fmodel_root)
                self.all_textures[tex_path] = {
                    'local_file': local_file,
                    'role': role,
                    'srgb': settings['srgb'],
                    'compression': settings['compression'],
                    'param_name': param_name,
                    'source': mi_path,
                }

        # Register this MI (after parent, before children)
        self.material_instances.append({
            'ue_path': mi_path,
            'parent_path': parent_path,
            'json_data': json_data,
            'tex_params': tex_params,
            'scalar_params': scalar_params,
        })

    def _resolve_master_material(self, mat_path, json_path, json_data):
        """Register a root Master Material and extract its texture param definitions.
        If the material has no TextureParameters (non-parameterized material),
        fall back to ReferencedTextures to collect direct texture references.
        """
        if mat_path in self.master_materials:
            return

        tex_params = get_master_material_tex_params(json_data)

        # Flag: whether this material uses TextureSampleParameter2D (parameterized)
        # or direct texture references (non-parameterized)
        has_tex_params = len(tex_params) > 0

        self.master_materials[mat_path] = {
            'json_path': json_path,
            'json_data': json_data,
            'tex_params': tex_params,
            'has_tex_params': has_tex_params,
            'direct_textures': [],  # populated below for non-parameterized materials
        }

        if has_tex_params:
            # Parameterized material: collect default textures from TextureValues
            for tp in tex_params:
                default_tex_name = tp['default_tex_filename']
                if not default_tex_name:
                    continue
                # Try to find it in TextureValues AssetPathName
                entries = json_data if isinstance(json_data, list) else [json_data]
                for e in entries:
                    cached = e.get('CachedExpressionData', {})
                    for tv in cached.get('TextureValues', []):
                        asset_path = norm_path(tv.get('AssetPathName', ''))
                        if asset_path and os.path.basename(asset_path).split('.')[0] == default_tex_name:
                            if asset_path not in self.all_textures:
                                local_file = find_texture_png(asset_path, self.fmodel_root)
                                role = determine_texture_role(tp['param_name'], default_tex_name)
                                settings = ROLE_SETTINGS[role]
                                self.all_textures[asset_path] = {
                                    'local_file': local_file,
                                    'role': role,
                                    'srgb': settings['srgb'],
                                    'compression': settings['compression'],
                                    'param_name': tp['param_name'],
                                    'source': mat_path,
                                }
                            break
                    break
        else:
            # Non-parameterized material: fall back to ReferencedTextures
            # These materials reference textures directly (no TextureSampleParameter2D nodes)
            entries = json_data if isinstance(json_data, list) else [json_data]
            direct_textures = []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                # ReferencedTextures appears in both top-level and CachedExpressionData
                ref_texs = e.get('ReferencedTextures', [])
                if not ref_texs:
                    cached = e.get('CachedExpressionData', {})
                    ref_texs = cached.get('ReferencedTextures', [])
                for rt in ref_texs:
                    obj_path = rt.get('ObjectPath', '')
                    if not obj_path or not obj_path.startswith('/Game/'):
                        continue
                    tex_path = norm_path(obj_path)
                    tex_filename = os.path.basename(tex_path).split('.')[0]
                    role = guess_role_from_suffix(tex_filename) or 'diffuse'
                    settings = ROLE_SETTINGS[role]

                    if tex_path not in self.all_textures:
                        local_file = find_texture_png(tex_path, self.fmodel_root)
                        self.all_textures[tex_path] = {
                            'local_file': local_file,
                            'role': role,
                            'srgb': settings['srgb'],
                            'compression': settings['compression'],
                            'param_name': tex_filename,  # use filename as param name
                            'source': mat_path,
                        }

                    direct_textures.append({
                        'tex_ue_path': tex_path,
                        'param_name': tex_filename,
                        'role': role,
                    })

                self.master_materials[mat_path]['direct_textures'] = direct_textures
                break

            print(f"    Master Material (non-parameterized): {mat_path} ({len(direct_textures)} direct textures)")
            for dt in direct_textures:
                print(f"      - {dt['param_name']} ({dt['role']})")

        if has_tex_params:
            print(f"    Master Material: {mat_path} ({len(tex_params)} texture params)")

        print(f"    Master Material: {mat_path} ({len(tex_params)} texture params)")

    def _find_root_master(self, mi_path):
        """Find the root Master Material path for an MI by checking parent chain."""
        # Check if any master material is in this MI's ancestry
        for mi in self.material_instances:
            if mi['ue_path'] == mi_path:
                # Walk up the parent chain
                current = mi['parent_path']
                while current:
                    if current in self.master_materials:
                        return current
                    # Find parent MI
                    found = False
                    for pmi in self.material_instances:
                        if pmi['ue_path'] == current:
                            current = pmi['parent_path']
                            found = True
                            break
                    if not found:
                        # parent might be a Master Material we haven't registered yet
                        if current in self.master_materials:
                            return current
                        return None
                return None
        return None


# ======================== UE Command Builders ========================

class UECommandBuilder:
    """Builds Python command strings for UE Remote Execution."""

    @staticmethod
    def ensure_directory(ue_dir):
        return f'''
import unreal
unreal.EditorAssetLibrary.make_directory("{ue_dir}")
result = "ok"
'''.strip()

    @staticmethod
    def asset_exists(ue_path):
        """Check if asset exists. Use with mode='eval'."""
        return f'__import__("unreal").EditorAssetLibrary.does_asset_exist("{ue_path}")'

    @staticmethod
    def import_texture(local_file, dest_path, srgb, compression, role):
        """Import a texture file and set compression/sRGB."""
        name = os.path.basename(dest_path)
        dest_dir = os.path.dirname(dest_path).replace('\\', '/')
        local_fwd = local_file.replace('\\', '/')
        srgb_str = 'True' if srgb else 'False'

        L = []
        def add(s=''):
            L.append('    ' + s if s else '')

        add('import unreal')
        add('task = unreal.AssetImportTask()')
        add('task.set_editor_property("automated", True)')
        add(f'task.set_editor_property("filename", r"{local_fwd}")')
        add(f'task.set_editor_property("destination_path", "{dest_dir}")')
        add(f'task.set_editor_property("destination_name", "{name}")')
        add('task.set_editor_property("replace_existing", True)')
        add('task.set_editor_property("save", True)')
        add('unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])')
        add(f'tex_path = "{dest_dir}/{name}"')
        add('texture = unreal.load_asset(tex_path)')
        add('if not (texture and isinstance(texture, unreal.Texture)):')
        add(f'    _suffix = "_" + "{name}".rsplit("_", 1)[-1] if "_" in "{name}" else ""')
        add(f'    for ap in unreal.EditorAssetLibrary.list_assets("{dest_dir}", recursive=False):')
        add('        ao = unreal.load_asset(ap)')
        add('        if ao and isinstance(ao, unreal.Texture) and (_suffix == "" or ao.get_name().endswith(_suffix)):')
        add('            texture = ao')
        add('            tex_path = ao.get_path_name()')
        add('            break')
        add('if not (texture and isinstance(texture, unreal.Texture)):')
        add('    return "ERROR: Texture import failed"')
        add(f'texture.set_editor_property("compression_settings", {compression})')
        add(f'texture.set_editor_property("srgb", {srgb_str})')
        add('unreal.EditorAssetLibrary.save_asset(texture.get_path_name())')
        add(f'return "OK: %s (role={role})" % texture.get_name()')

        body = '\n'.join(L)
        return f'''import unreal, traceback
def _run():
{body}
try:
    result = _run()
    print("RESULT::" + str(result))
except Exception as e:
    traceback.print_exc()
    print("RESULT::ERROR: " + str(e))
finally:
    import gc as _gc; _gc.collect()
    try: unreal.collect_garbage(unreal.GarbageCollectionKeepFlags.KEEP_FLAGS)
    except: pass
'''

    @staticmethod
    def check_and_fix_texture(dest_path, srgb, compression, role):
        """Check if existing texture's sRGB and compression match expected; fix if wrong.
        Returns a command string that prints RESULT:: with summary.
        """
        srgb_str = 'True' if srgb else 'False'

        L = []
        def add(s=''):
            L.append('    ' + s if s else '')

        add('import unreal')
        add(f'tex = unreal.load_asset("{dest_path}")')
        add('if not tex:')
        add('    # UE may have truncated the asset name on import; search directory by suffix')
        add(f'    _pkg = "{dest_path}"')
        add('    _pkg_dir = _pkg.rsplit("/", 1)[0] if "/" in _pkg else ""')
        add('    _exp_name = _pkg.rsplit("/", 1)[-1] if "/" in _pkg else _pkg')
        add('    _suffix = "_" + _exp_name.rsplit("_", 1)[-1] if "_" in _exp_name else ""')
        add('    if _pkg_dir and _suffix:')
        add('        for _ap in unreal.EditorAssetLibrary.list_assets(_pkg_dir, recursive=False):')
        add('            _ao = unreal.load_asset(_ap)')
        add('            if _ao and isinstance(_ao, unreal.Texture) and _ao.get_name().endswith(_suffix):')
        add('                tex = _ao')
        add('                break')
        add('if not tex:')
        add(f'    return "ERROR: texture not found: {dest_path}"')
        add('cur_srgb = tex.get_editor_property("srgb")')
        add('cur_comp = tex.get_editor_property("compression_settings")')
        add(f'expected_srgb = {srgb_str}')
        add(f'expected_comp = {compression}')
        add('fixed = []')
        add('if cur_srgb != expected_srgb:')
        add('    tex.set_editor_property("srgb", expected_srgb)')
        add(f'    fixed.append("srgb: %s -> %s" % (cur_srgb, expected_srgb))')
        add('if cur_comp != expected_comp:')
        add('    tex.set_editor_property("compression_settings", expected_comp)')
        add(f'    fixed.append("compression: %s -> %s" % (cur_comp, expected_comp))')
        add('if fixed:')
        add('    unreal.EditorAssetLibrary.save_asset(tex.get_path_name())')
        add(f'    return "FIXED (%d): %s" % (len(fixed), "; ".join(fixed))')
        add('else:')
        add(f'    return "OK: srgb=%s compression=%s" % (cur_srgb, cur_comp)')

        body = '\n'.join(L)
        return f'''import unreal, traceback
def _run():
{body}
try:
    result = _run()
    print("RESULT::" + str(result))
except Exception as e:
    traceback.print_exc()
    print("RESULT::ERROR: " + str(e))
finally:
    import gc as _gc; _gc.collect()
    try: unreal.collect_garbage(unreal.GarbageCollectionKeepFlags.KEEP_FLAGS)
    except: pass
'''

    @staticmethod
    def create_master_material(dest_path, tex_param_defs, texture_ue_paths=None):
        """Create a Master Material with TextureSampleParameter2D nodes.
        tex_param_defs: list of {param_name, role}
        texture_ue_paths: optional dict {param_name: ue_texture_path} for non-parameterized
                         materials that reference textures directly. When provided, the
                         actual imported texture is loaded and set on the node instead of
                         the engine default texture.
        Only the first diffuse/normal/orm param gets connected to outputs.
        Built as a flat _run() function body with consistent 4-space indentation.
        """
        name = os.path.basename(dest_path)
        dest_dir = os.path.dirname(dest_path).replace('\\', '/')

        L = []  # lines inside def _run(): (4-space indented)
        def add(s=''):
            L.append('    ' + s if s else '')

        add('import unreal')
        add(f'if unreal.EditorAssetLibrary.does_asset_exist("{dest_path}"):')
        add(f'    unreal.EditorAssetLibrary.delete_asset("{dest_path}")')
        add('at = unreal.AssetToolsHelpers.get_asset_tools()')
        add(f'mat = at.create_asset("{name}", "{dest_dir}", unreal.Material, unreal.MaterialFactoryNew())')
        add('if not mat:')
        add('    return "ERROR: Failed to create material"')

        connected = {'diffuse': False, 'normal': False, 'orm': False}
        y = 0
        for tp in tex_param_defs:
            pname = tp['param_name']
            role = tp['role']
            settings = ROLE_SETTINGS[role]
            sampler_type = settings['sampler_type']
            connections = settings['connections']
            # escape quotes in param name
            safe_pname = pname.replace('"', '\\"')
            add(f'ts = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -400, {y})')
            add(f'ts.set_editor_property("parameter_name", "{safe_pname}")')
            add(f'ts.set_editor_property("sampler_type", {sampler_type})')
            # Set the default texture: use actual imported texture if available
            # (non-parameterized materials), otherwise use engine default.
            tex_ue_path = (texture_ue_paths or {}).get(pname)
            if tex_ue_path:
                add(f'_deftex = unreal.load_asset("{tex_ue_path}")')
            elif role == 'normal':
                add('_deftex = unreal.load_asset("/Engine/EngineMaterials/DefaultNormal")')
            else:
                add('_deftex = unreal.load_asset("/Engine/EngineMaterials/DefaultDiffuse")')
            add('if _deftex:')
            add('    ts.set_editor_property("texture", _deftex)')
            if role in connected and not connected[role] and connections:
                for channel, mp_prop in connections:
                    add(f'unreal.MaterialEditingLibrary.connect_material_property(ts, "{channel}", {mp_prop})')
                connected[role] = True
            y += 200

        # Fallback default connections for unconnected outputs
        if not connected['diffuse']:
            add('root_diffuse = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionConstant3Vector, -800, 0)')
            add('root_diffuse.set_editor_property("Constant", unreal.LinearColor(0.5, 0.5, 0.5, 1.0))')
            add('unreal.MaterialEditingLibrary.connect_material_property(root_diffuse, "RGB", unreal.MaterialProperty.MP_BASE_COLOR)')
        if not connected['orm']:
            add('root_rough = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionConstant, -800, 400)')
            add('root_rough.set_editor_property("R", 0.5)')
            add('unreal.MaterialEditingLibrary.connect_material_property(root_rough, "", unreal.MaterialProperty.MP_ROUGHNESS)')

        add('unreal.MaterialEditingLibrary.recompile_material(mat)')
        add(f'unreal.EditorAssetLibrary.save_asset("{dest_path}")')
        add(f'return "OK: Created master material %s with {len(tex_param_defs)} params" % mat.get_name()')

        body = '\n'.join(L)
        return f'''import unreal, traceback
def _run():
{body}
try:
    result = _run()
    print("RESULT::" + str(result))
except Exception as e:
    traceback.print_exc()
    print("RESULT::ERROR: " + str(e))
finally:
    import gc as _gc; _gc.collect()
    try: unreal.collect_garbage(unreal.GarbageCollectionKeepFlags.KEEP_FLAGS)
    except: pass
'''

    @staticmethod
    def create_material_instance(dest_path, parent_path, tex_values, scalar_values):
        """Create a MaterialInstanceConstant with parent and parameter overrides.
        tex_values: list of {param_name, texture_ue_path}
        scalar_values: list of {param_name, value}
        Built as a flat _run() function body with consistent 4-space indentation.
        """
        name = os.path.basename(dest_path)
        dest_dir = os.path.dirname(dest_path).replace('\\', '/')

        L = []
        def add(s=''):
            L.append('    ' + s if s else '')

        add('import unreal')
        add(f'if unreal.EditorAssetLibrary.does_asset_exist("{dest_path}"):')
        add(f'    unreal.EditorAssetLibrary.delete_asset("{dest_path}")')
        add(f'parent = unreal.load_asset("{parent_path}")')
        add('if not parent:')
        add(f'    return "ERROR: Parent not found: {parent_path}"')
        add('at = unreal.AssetToolsHelpers.get_asset_tools()')
        add('factory = unreal.MaterialInstanceConstantFactoryNew()')
        add(f'mic = at.create_asset("{name}", "{dest_dir}", unreal.MaterialInstanceConstant, factory)')
        add('if not mic:')
        add('    return "ERROR: Failed to create MIC"')
        add('unreal.MaterialEditingLibrary.set_material_instance_parent(mic, parent)')

        for tv in tex_values:
            pname = tv['param_name'].replace('"', '\\"')
            tex_path = tv['texture_ue_path']
            tex_obj = tex_path + '.' + os.path.basename(tex_path)
            add(f'_t = unreal.load_asset("{tex_obj}")')
            add(f'if _t:')
            add(f'    unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(mic, "{pname}", _t)')
            add(f'else:')
            add(f'    print("WARN: texture not found for param {pname}: {tex_obj}")')

        for sv in scalar_values:
            pname = sv['param_name'].replace('"', '\\"')
            val = sv['value']
            add(f'unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(mic, "{pname}", {val})')

        add('unreal.MaterialEditingLibrary.update_material_instance(mic)')
        add(f'unreal.EditorAssetLibrary.save_asset("{dest_path}")')
        add('return "OK: Created MI %s (parent: %s)" % (mic.get_name(), parent.get_name())')

        body = '\n'.join(L)
        return f'''import unreal, traceback
def _run():
{body}
try:
    result = _run()
    print("RESULT::" + str(result))
except Exception as e:
    traceback.print_exc()
    print("RESULT::ERROR: " + str(e))
finally:
    import gc as _gc; _gc.collect()
    try: unreal.collect_garbage(unreal.GarbageCollectionKeepFlags.KEEP_FLAGS)
    except: pass
'''

    @staticmethod
    def check_and_fix_mi(dest_path, tex_values):
        """Check if existing MI's texture parameters match expected values; fix mismatches.
        tex_values: list of {param_name, texture_ue_path}
        Returns a command string that prints RESULT:: with summary.
        """
        # Build expected dict as Python literal
        expected_items = []
        for tv in tex_values:
            pname = tv['param_name'].replace('"', '\\"')
            tex_path = tv['texture_ue_path']
            tex_obj = tex_path + '.' + os.path.basename(tex_path)
            expected_items.append(f'    "{pname}": "{tex_obj}",')
        expected_dict_str = '\n'.join(expected_items) if expected_items else ''

        L = []
        def add(s=''):
            L.append('    ' + s if s else '')

        add('import unreal')
        add(f'mic = unreal.load_asset("{dest_path}")')
        add('if not mic:')
        add(f'    return "ERROR: MI not found: {dest_path}"')
        add('expected = {')
        if expected_dict_str:
            for line in expected_dict_str.split('\n'):
                add(line)
        add('}')
        add('current = {}')
        add('tpv = mic.get_editor_property("texture_parameter_values")')
        add('for entry in tpv:')
        add('    pinfo = entry.get_editor_property("parameter_info")')
        add('    pname = pinfo.get_editor_property("name")')
        add('    ptex = entry.get_editor_property("parameter_value")')
        add('    current[pname] = ptex.get_path_name() if ptex else ""')
        add('mismatches = []')
        add('fixed = []')
        add('failed = []')
        add('for pname, expected_path in expected.items():')
        add('    cur = current.get(pname, "")')
        add('    print("DEBUG: param=%s | current=%s | expected=%s" % (pname, cur, expected_path))')
        add('    # Compare by package path (strip .Object suffix) for robustness')
        add('    cur_pkg = cur.rsplit(".", 1)[0] if "." in cur else cur')
        add('    exp_pkg = expected_path.rsplit(".", 1)[0] if "." in expected_path else expected_path')
        add('    if cur_pkg != exp_pkg:')
        add('        mismatches.append(pname)')
        add('        tex = unreal.load_asset(expected_path)')
        add('        if not tex:')
        add('            # Fallback: try loading by package path (without .Object suffix)')
        add('            tex = unreal.load_asset(exp_pkg)')
        add('        if not tex:')
        add('            # Fallback 2: search the package directory for a texture matching by suffix')
        add('            # UE truncates long asset names on import, so exact match fails.')
        add('            # Match by role suffix (_D, _N, _ORM) instead.')
        add('            exp_name = exp_pkg.rsplit("/", 1)[-1] if "/" in exp_pkg else exp_pkg')
        add('            # Extract suffix: last segment after final underscore (e.g. D, N, ORM)')
        add('            exp_suffix = "_" + exp_name.rsplit("_", 1)[-1] if "_" in exp_name else ""')
        add('            pkg_dir = exp_pkg.rsplit("/", 1)[0] if "/" in exp_pkg else ""')
        add('            if pkg_dir and exp_suffix:')
        add('                for ap in unreal.EditorAssetLibrary.list_assets(pkg_dir, recursive=False):')
        add('                    ao = unreal.load_asset(ap)')
        add('                    if ao and isinstance(ao, unreal.Texture) and ao.get_name().endswith(exp_suffix):')
        add('                        tex = ao')
        add('                        break')
        add('        if tex:')
        add('            unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(mic, pname, tex)')
        add('            fixed.append("%s: %s -> %s" % (pname, cur, tex.get_path_name()))')
        add('        else:')
        add('            # Diagnostics: why did all fallbacks fail?')
        add('            diag = []')
        add('            exists_full = unreal.EditorAssetLibrary.does_asset_exist(expected_path)')
        add('            exists_pkg = unreal.EditorAssetLibrary.does_asset_exist(exp_pkg) if exp_pkg != expected_path else exists_full')
        add('            diag.append("does_asset_exist(full=%s, pkg=%s)" % (exists_full, exists_pkg))')
        add('            if pkg_dir:')
        add('                try:')
        add('                    dir_assets = unreal.EditorAssetLibrary.list_assets(pkg_dir, recursive=False)')
        add('                    if dir_assets:')
        add('                        diag.append("dir has %d assets: %s" % (len(dir_assets), ", ".join(dir_assets)))')
        add('                    else:')
        add('                        diag.append("dir is empty or does not exist")')
        add('                except Exception as ex:')
        add('                    diag.append("dir listing failed: %s" % str(ex))')
        add('            else:')
        add('                diag.append("no package dir")')
        add('            failed.append("%s: %s [%s]" % (pname, expected_path, "; ".join(diag)))')
        add('            print("ERROR: texture not found for param %s: %s | %s" % (pname, expected_path, "; ".join(diag)))')
        add('if fixed:')
        add('    unreal.MaterialEditingLibrary.update_material_instance(mic)')
        add(f'    unreal.EditorAssetLibrary.save_asset("{dest_path}")')
        add('parts = []')
        add('if fixed: parts.append("fixed %d [%s]" % (len(fixed), "; ".join(fixed)))')
        add('if failed: parts.append("FAILED %d [%s]" % (len(failed), "; ".join(failed)))')
        add('if mismatches and not fixed and not failed: parts.append("unmatched but no action taken")')
        add('if not mismatches:')
        add('    parts.append("all %d match" % len(expected))')
        add('return " | ".join(parts) if parts else "no expected params"')

        body = '\n'.join(L)
        return f'''import unreal, traceback
def _run():
{body}
try:
    result = _run()
    print("RESULT::" + str(result))
except Exception as e:
    traceback.print_exc()
    print("RESULT::ERROR: " + str(e))
finally:
    import gc as _gc; _gc.collect()
    try: unreal.collect_garbage(unreal.GarbageCollectionKeepFlags.KEEP_FLAGS)
    except: pass
'''

    @staticmethod
    def check_and_fix_master_material(dest_path, expected_textures):
        """Check if existing Master Material's TextureSampleParameter2D nodes have
        correct default textures; fix if wrong. Only replaces textures, never touches
        material connections (user may have customized them).
        Uses MaterialEditingLibrary API instead of protected 'expressions' property.
        expected_textures: dict {param_name: expected_texture_ue_path}
        """
        # Build expected dict as Python literal
        expected_items = []
        for pname, tex_path in expected_textures.items():
            safe_pn = pname.replace('"', '\\"')
            safe_tp = tex_path.replace('"', '\\"')
            tex_obj = safe_tp + '.' + os.path.basename(safe_tp)
            expected_items.append(f'    "{safe_pn}": "{tex_obj}",')
        expected_dict_str = '\n'.join(expected_items) if expected_items else ''

        L = []
        def add(s=''):
            L.append('    ' + s if s else '')

        add('import unreal')
        add(f'mat = unreal.load_asset("{dest_path}")')
        add('if not mat:')
        add(f'    return "ERROR: master material not found: {dest_path}"')
        add('expected = {')
        if expected_dict_str:
            for line in expected_dict_str.split('\n'):
                add(line)
        add('}')
        add('# Get all texture parameter names from the material via MaterialEditingLibrary')
        add('all_param_names = unreal.MaterialEditingLibrary.get_texture_parameter_names(mat)')
        add('mismatches = []')
        add('fixed = []')
        add('failed = []')
        add('for pname, expected_path in expected.items():')
        add('    # Read current default texture via MaterialEditingLibrary API')
        add('    cur_tex = unreal.MaterialEditingLibrary.get_material_default_texture_parameter_value(mat, pname)')
        add('    cur = cur_tex.get_path_name() if cur_tex else ""')
        add('    print("DEBUG: param=%s | current=%s | expected=%s" % (pname, cur, expected_path))')
        add('    cur_pkg = cur.rsplit(".", 1)[0] if "." in cur else cur')
        add('    exp_pkg = expected_path.rsplit(".", 1)[0] if "." in expected_path else expected_path')
        add('    if cur_pkg != exp_pkg:')
        add('        mismatches.append(pname)')
        add('        tex = unreal.load_asset(expected_path)')
        add('        if not tex:')
        add('            tex = unreal.load_asset(exp_pkg)')
        add('        if not tex:')
        add('            # Fallback: search directory by suffix (UE truncates long names)')
        add('            exp_name = exp_pkg.rsplit("/", 1)[-1] if "/" in exp_pkg else exp_pkg')
        add('            exp_suffix = "_" + exp_name.rsplit("_", 1)[-1] if "_" in exp_name else ""')
        add('            pkg_dir = exp_pkg.rsplit("/", 1)[0] if "/" in exp_pkg else ""')
        add('            if pkg_dir and exp_suffix:')
        add('                for ap in unreal.EditorAssetLibrary.list_assets(pkg_dir, recursive=False):')
        add('                    ao = unreal.load_asset(ap)')
        add('                    if ao and isinstance(ao, unreal.Texture) and ao.get_name().endswith(exp_suffix):')
        add('                        tex = ao')
        add('                        break')
        add('        if tex:')
        add('            # Find the TSP2D node via ObjectIterator (expressions property is protected)')
        add('            target_node = None')
        add('            for obj in unreal.ObjectIterator():')
        add('                if obj.get_outer() == mat and isinstance(obj, unreal.MaterialExpressionTextureSampleParameter2D):')
        add('                    if obj.get_editor_property("parameter_name") == pname:')
        add('                        target_node = obj')
        add('                        break')
        add('            if target_node:')
        add('                target_node.set_editor_property("texture", tex)')
        add('                fixed.append("%s: %s -> %s" % (pname, cur, tex.get_path_name()))')
        add('            else:')
        add('                failed.append("%s: TSP2D node not found in material" % pname)')
        add('        else:')
        add('            diag = []')
        add('            exists_full = unreal.EditorAssetLibrary.does_asset_exist(expected_path)')
        add('            exists_pkg = unreal.EditorAssetLibrary.does_asset_exist(exp_pkg) if exp_pkg != expected_path else exists_full')
        add('            diag.append("does_asset_exist(full=%s, pkg=%s)" % (exists_full, exists_pkg))')
        add('            if pkg_dir:')
        add('                try:')
        add('                    dir_assets = unreal.EditorAssetLibrary.list_assets(pkg_dir, recursive=False)')
        add('                    if dir_assets:')
        add('                        diag.append("dir has %d assets: %s" % (len(dir_assets), ", ".join(dir_assets)))')
        add('                    else:')
        add('                        diag.append("dir is empty or does not exist")')
        add('                except Exception as ex:')
        add('                    diag.append("dir listing failed: %s" % str(ex))')
        add('            else:')
        add('                diag.append("no package dir")')
        add('            failed.append("%s: %s [%s]" % (pname, expected_path, "; ".join(diag)))')
        add('if fixed:')
        add('    unreal.MaterialEditingLibrary.recompile_material(mat)')
        add('    unreal.EditorAssetLibrary.save_asset(mat.get_path_name())')
        add('parts = []')
        add('if fixed: parts.append("fixed %d [%s]" % (len(fixed), "; ".join(fixed)))')
        add('if failed: parts.append("FAILED %d [%s]" % (len(failed), "; ".join(failed)))')
        add('if mismatches and not fixed and not failed: parts.append("unmatched but no action taken")')
        add('if not mismatches:')
        add('    parts.append("all %d match" % len(expected))')
        add('return " | ".join(parts) if parts else "no expected params"')

        body = '\n'.join(L)
        return f'''import unreal, traceback
def _run():
{body}
try:
    result = _run()
    print("RESULT::" + str(result))
except Exception as e:
    traceback.print_exc()
    print("RESULT::ERROR: " + str(e))
finally:
    import gc as _gc; _gc.collect()
    try: unreal.collect_garbage(unreal.GarbageCollectionKeepFlags.KEEP_FLAGS)
    except: pass
'''

    @staticmethod
    def import_mesh(local_file, dest_path):
        """Import a GLB mesh into UE."""
        name = os.path.basename(dest_path)
        dest_dir = os.path.dirname(dest_path).replace('\\', '/')
        local_fwd = local_file.replace('\\', '/')

        L = []
        def add(s=''):
            L.append('    ' + s if s else '')

        add('import unreal')
        add('task = unreal.AssetImportTask()')
        add('task.set_editor_property("automated", True)')
        add(f'task.set_editor_property("filename", r"{local_fwd}")')
        add(f'task.set_editor_property("destination_path", "{dest_dir}")')
        add(f'task.set_editor_property("destination_name", "{name}")')
        add('task.set_editor_property("replace_existing", True)')
        add('task.set_editor_property("save", True)')
        add('unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])')
        add(f'expected_path = "{dest_dir}/{name}"')
        add('imported = None')
        # glTF Interchange importer places the mesh in a subfolder like
        # <dest_dir>/<name>/StaticMeshes/<name>. Use imported_object_paths to find it.
        add('try:')
        add('    obj_paths = list(task.get_editor_property("imported_object_paths"))')
        add('except Exception:')
        add('    obj_paths = []')
        add('for op in obj_paths:')
        add('    ao = unreal.load_asset(op)')
        add('    if ao and isinstance(ao, unreal.StaticMesh):')
        add('        imported = ao')
        add('        break')
        # Fallback: recursive scan under dest_dir
        add('if not imported:')
        add(f'    for ap in unreal.EditorAssetLibrary.list_assets("{dest_dir}", recursive=True):')
        add('        ao = unreal.load_asset(ap)')
        add('        if ao and isinstance(ao, unreal.StaticMesh):')
        add('            imported = ao')
        add('            break')
        # Clean up: delete any non-mesh assets (materials, textures) created by GLB importer
        add('if obj_paths:')
        add('    for op in obj_paths:')
        add('        ao = unreal.load_asset(op)')
        add('        if ao and not isinstance(ao, unreal.StaticMesh):')
        add('            try: unreal.EditorAssetLibrary.delete_asset(op.split(".")[0])')
        add('            except Exception: pass')
        add('if not imported:')
        add('    return "ERROR: Mesh import failed (no StaticMesh found)"')
        # Rename to expected flat path if not already there
        add('cur_path = imported.get_path_name().split(".")[0]')
        add('if cur_path != expected_path:')
        add('    if unreal.EditorAssetLibrary.does_asset_exist(expected_path):')
        add('        unreal.EditorAssetLibrary.delete_asset(expected_path)')
        add('    unreal.EditorAssetLibrary.rename_asset(cur_path, expected_path)')
        add('    imported = unreal.load_asset(expected_path)')
        add('unreal.EditorAssetLibrary.save_asset(expected_path)')
        add('return "OK: %s" % imported.get_name()')

        body = '\n'.join(L)
        return f'''import unreal, traceback
def _run():
{body}
try:
    result = _run()
    print("RESULT::" + str(result))
except Exception as e:
    traceback.print_exc()
    print("RESULT::ERROR: " + str(e))
finally:
    import gc as _gc; _gc.collect()
    try: unreal.collect_garbage(unreal.GarbageCollectionKeepFlags.KEEP_FLAGS)
    except: pass
'''

    @staticmethod
    def assign_materials(mesh_path, sections):
        """Assign materials to a static mesh's sections.
        sections: list of {slot_index, material_dest_path}
        """
        L = []
        def add(s=''):
            L.append('    ' + s if s else '')

        add('import unreal')
        add(f'mesh = unreal.load_asset("{mesh_path}")')
        add('if not (mesh and isinstance(mesh, unreal.StaticMesh)):')
        add(f'    return "ERROR: Mesh not found: {mesh_path}"')
        for s in sections:
            idx = s['slot_index']
            mpath = s['material_dest_path']
            if not mpath:
                add(f'print("Slot {idx}: no material path")')
                continue
            add(f'_m = unreal.load_asset("{mpath}")')
            add(f'if _m:')
            add(f'    mesh.set_material({idx}, _m)')
            add(f'else:')
            add(f'    print("Slot {idx}: material not found: {mpath}")')
        add(f'unreal.EditorAssetLibrary.save_asset("{mesh_path}")')
        add('return "OK: Assigned materials to %s" % mesh.get_name()')

        body = '\n'.join(L)
        return f'''import unreal, traceback
def _run():
{body}
try:
    result = _run()
    print("RESULT::" + str(result))
except Exception as e:
    traceback.print_exc()
    print("RESULT::ERROR: " + str(e))
finally:
    import gc as _gc; _gc.collect()
    try: unreal.collect_garbage(unreal.GarbageCollectionKeepFlags.KEEP_FLAGS)
    except: pass
'''


# ======================== Import Pipeline ========================

class MeshImportPipeline:
    """Orchestrates the 5-step import pipeline."""

    def __init__(self, fmodel_root, content_root, ue_host='239.0.0.1', ue_port=6766):
        self.fmodel_root = fmodel_root
        self.content_root = content_root
        self.ue_host = ue_host
        self.ue_port = ue_port
        self.client = None

    def log(self, msg):
        print(msg)

    def run(self, mesh_json_path):
        """Run the full import pipeline for a mesh JSON file."""
        self.log(f"\n{'='*60}")
        self.log(f"Import Pipeline: {os.path.basename(mesh_json_path)}")
        self.log(f"{'='*60}")

        # Connect to UE
        self.log("\nConnecting to UE...")
        self.client = UERemoteExec(self.ue_host, self.ue_port)
        if not self.client.connect(timeout=15):
            self.log("ERROR: Cannot connect to UE. Ensure UE is running with Python Remote Execution.")
            return False
        self.log("OK: Connected to UE")

        try:
            # Step 1: Import GLB mesh
            self._step1_import_glb(mesh_json_path)

            # Step 2: Collect materials & textures
            resolver = MaterialChainResolver(self.fmodel_root)
            deps = resolver.resolve(mesh_json_path)

            # Step 3: Import all textures
            self._step3_import_textures(deps['all_textures'])

            # Step 4: Create Master Materials
            self._step4_create_master_materials(deps['master_materials'], deps['all_textures'])

            # Step 5: Create MaterialInstanceConstants
            self._step5_create_mis(deps['material_instances'], deps['master_materials'])

            # Step 6: Assign materials to mesh sections
            self._step6_assign_materials(mesh_json_path, deps['sections'])

            self.log(f"\n{'='*60}")
            self.log("Import pipeline complete!")
            self.log(f"{'='*60}")
            return True
        except Exception as e:
            self.log(f"ERROR: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            self.client.disconnect()

    @staticmethod
    def _parse_output(r):
        """Extract combined stdout text from a command result's 'output' field.
        output is a list of {type, output} dicts. Returns joined string.
        """
        out = r.get('output', '')
        if isinstance(out, list):
            parts = []
            for item in out:
                if isinstance(item, dict):
                    parts.append(str(item.get('output', '')))
                else:
                    parts.append(str(item))
            return ''.join(parts)
        return str(out)

    def _run_cmd(self, cmd, mode='exec', timeout=120):
        """Execute a command in UE. Commands should print('RESULT::' + result).
        Returns dict with success/result_text/error/output_text.
        In exec mode, the 'result' variable is NOT returned by UE, so we parse
        the printed RESULT:: marker from stdout instead.
        """
        if mode == 'exec' and 'RESULT::' not in cmd:
            cmd = cmd + '\ntry:\n    print("RESULT::" + str(result))\nexcept Exception:\n    print("RESULT::<no result var>")'
        r = self.client.run_command(cmd, mode=mode, timeout=timeout)
        out_text = self._parse_output(r)
        result_text = ''
        for line in out_text.splitlines():
            line = line.strip()
            if line.startswith('RESULT::'):
                result_text = line[len('RESULT::'):]
        err = r.get('error', '') or ''
        if not r.get('success') and err:
            self.log(f"  WARNING: {err[:300]}")
        elif err:
            # Python exception surfaced even with success flag
            self.log(f"  WARNING (exception): {err[:300]}")
        return {
            'success': r.get('success', False) and not err,
            'result_text': result_text,
            'output_text': out_text,
            'error': err,
        }

    def _run_eval(self, cmd, timeout=30):
        """Execute an eval command and return the result string."""
        r = self.client.run_command(cmd, mode='eval', timeout=timeout)
        if r.get('success'):
            return str(r.get('result', '')).strip()
        return ''

    def _step1_import_glb(self, mesh_json_path):
        """Step 1: Import GLB mesh from Mesh.json."""
        self.log("\n=== Step 1: Importing GLB mesh ===")
        mesh_data = load_json(mesh_json_path)
        entries = mesh_data if isinstance(mesh_data, list) else [mesh_data]
        mesh_ue_path = None
        for e in entries:
            if isinstance(e, dict) and e.get('Type') == 'StaticMesh':
                pkg = e.get('Package', '')
                if pkg:
                    mesh_ue_path = pkg
                    break
        if not mesh_ue_path:
            # Derive from file path
            rel = os.path.relpath(mesh_json_path, self.fmodel_root).replace('\\', '/')
            mesh_ue_path = '/Game/' + rel.replace('.json', '')

        glb_file = find_glb(mesh_ue_path, self.fmodel_root)
        if not glb_file:
            self.log(f"  ERROR: GLB file not found for {mesh_ue_path}")
            return

        dest = ue_path_to_import_dest(mesh_ue_path, self.content_root)
        self.log(f"  Importing: {os.path.basename(glb_file)} -> {dest}")
        cmd = UECommandBuilder.import_mesh(glb_file, dest)
        r = self._run_cmd(cmd, timeout=300)
        self.log(f"  {r.get('result_text','')[:150] or r.get('output_text','')[:200]}")

    def _step3_import_textures(self, all_textures):
        """Step 3: Import all collected textures with correct sRGB/compression."""
        self.log(f"\n=== Step 3: Importing {len(all_textures)} textures ===")
        imported = 0
        skipped = 0
        failed = 0

        for tex_path, info in all_textures.items():
            if not info['local_file']:
                self.log(f"  SKIP (no file): {os.path.basename(tex_path)}")
                skipped += 1
                continue

            dest = ue_path_to_import_dest(tex_path, self.content_root)

            # Check if already exists
            exists = self._run_eval(UECommandBuilder.asset_exists(dest))
            if exists.lower() in ('true', '1'):
                # Verify sRGB and compression match expected settings; fix if wrong
                self.log(f"  CHECK (exists): {os.path.basename(tex_path)} (role={info['role']}, srgb={info['srgb']})")
                cmd = UECommandBuilder.check_and_fix_texture(
                    dest, info['srgb'], info['compression'], info['role']
                )
                r = self._run_cmd(cmd, timeout=120)
                result_str = r.get('result_text', '')
                if 'FIXED' in result_str:
                    self.log(f"    {result_str[:200]}")
                elif 'OK' in result_str:
                    self.log(f"    {result_str[:150]}")
                elif 'ERROR' in result_str:
                    self.log(f"    FAILED: {result_str[:200]}")
                else:
                    self.log(f"    {result_str[:200] or r.get('output_text','')[:200]}")
                skipped += 1
                continue

            # asset_exists returned False, but texture may exist under a truncated name
            # Try check_and_fix first (it has directory-search fallback for truncated names)
            self.log(f"  Importing: {os.path.basename(tex_path)} (role={info['role']}, srgb={info['srgb']})")
            cmd = UECommandBuilder.check_and_fix_texture(
                dest, info['srgb'], info['compression'], info['role']
            )
            r = self._run_cmd(cmd, timeout=120)
            result_str = r.get('result_text', '')
            if 'OK' in result_str:
                # Texture exists (under truncated name) and settings are correct
                imported += 1
                self.log(f"    OK (exists truncated): {result_str[:150]}")
                continue
            elif 'FIXED' in result_str:
                # Texture exists but settings were wrong, now fixed
                imported += 1
                self.log(f"    FIXED (exists truncated): {result_str[:200]}")
                continue

            # Texture genuinely doesn't exist, do real import
            cmd = UECommandBuilder.import_texture(
                info['local_file'], dest, info['srgb'], info['compression'], info['role']
            )
            r = self._run_cmd(cmd, timeout=120)
            result_str = r.get('result_text', '')
            if 'OK' in result_str:
                imported += 1
                self.log(f"    {result_str[:150]}")
            else:
                self.log(f"    FAILED: {result_str[:150]} | {r.get('output_text','')[:200]}")
                failed += 1

        self.log(f"  Summary: {imported} imported, {skipped} skipped, {failed} failed")

    def _step4_create_master_materials(self, master_materials, all_textures):
        """Step 4: Create Master Materials with texture parameters matching JSON."""
        self.log(f"\n=== Step 4: Creating {len(master_materials)} Master Materials ===")
        for mat_path, info in master_materials.items():
            dest = ue_path_to_import_dest(mat_path, self.content_root)

            # Check if already exists
            exists = self._run_eval(UECommandBuilder.asset_exists(dest))
            if exists.lower() in ('true', '1'):
                # Build expected_textures for checking (same logic as creation below)
                expected_textures = {}
                if info.get('has_tex_params', True):
                    for tp in info['tex_params']:
                        pname = tp['param_name']
                        default_tex = tp['default_tex_filename']
                        # Find the texture UE path from all_textures by matching param_name or default_tex_filename
                        tex_ue_path = None
                        for tpath, tinfo in all_textures.items():
                            if tinfo.get('param_name') == pname or os.path.basename(tpath).split('.')[0] == default_tex:
                                tex_ue_path = tpath
                                break
                        if tex_ue_path:
                            tex_dest = ue_path_to_import_dest(tex_ue_path, self.content_root)
                            expected_textures[pname] = tex_dest
                else:
                    for dt in info.get('direct_textures', []):
                        pname = dt['param_name']
                        tex_dest = ue_path_to_import_dest(dt['tex_ue_path'], self.content_root)
                        expected_textures[pname] = tex_dest

                if expected_textures:
                    self.log(f"  CHECK (exists): {os.path.basename(mat_path)}")
                    cmd = UECommandBuilder.check_and_fix_master_material(dest, expected_textures)
                    r = self._run_cmd(cmd, timeout=120)
                    result_str = r.get('result_text', '')
                    if 'FIXED' in result_str:
                        self.log(f"    {result_str[:300]}")
                    elif 'OK' in result_str or 'match' in result_str:
                        self.log(f"    {result_str[:200]}")
                    elif 'ERROR' in result_str or 'FAILED' in result_str:
                        self.log(f"    FAILED: {result_str[:300]}")
                    else:
                        self.log(f"    {result_str[:300] or r.get('output_text','')[:200]}")
                else:
                    self.log(f"  SKIP (exists, no tex params): {os.path.basename(mat_path)}")
                continue

            # Build tex_param_defs with role determination
            tex_param_defs = []
            texture_ue_paths = {}

            if info.get('has_tex_params', True):
                # Parameterized material: use tex_params from CachedExpressionData
                for tp in info['tex_params']:
                    pname = tp['param_name']
                    default_tex = tp['default_tex_filename']
                    role = determine_texture_role(pname, default_tex)
                    tex_param_defs.append({
                        'param_name': pname,
                        'role': role,
                    })
            else:
                # Non-parameterized material: use direct_textures from ReferencedTextures
                for dt in info.get('direct_textures', []):
                    pname = dt['param_name']
                    role = dt['role']
                    tex_param_defs.append({
                        'param_name': pname,
                        'role': role,
                    })
                    # Map param name to imported texture UE path
                    tex_dest = ue_path_to_import_dest(dt['tex_ue_path'], self.content_root)
                    texture_ue_paths[pname] = tex_dest

            self.log(f"  Creating: {os.path.basename(mat_path)} ({len(tex_param_defs)} tex params)")
            for tpd in tex_param_defs:
                tex_tag = f" -> {texture_ue_paths[tpd['param_name']]}" if tpd['param_name'] in texture_ue_paths else ""
                self.log(f"    - {tpd['param_name']} ({tpd['role']}){tex_tag}")

            cmd = UECommandBuilder.create_master_material(dest, tex_param_defs, texture_ue_paths)
            r = self._run_cmd(cmd, timeout=120)
            result_str = r.get('result_text', '')
            self.log(f"    {result_str[:200] or r.get('output_text','')[:200]}")

    def _step5_create_mis(self, material_instances, master_materials):
        """Step 5: Create MaterialInstanceConstants with texture values from MI JSON."""
        self.log(f"\n=== Step 5: Creating {len(material_instances)} MaterialInstanceConstants ===")

        for mi in material_instances:
            mi_path = mi['ue_path']
            parent_path = mi['parent_path']
            dest = ue_path_to_import_dest(mi_path, self.content_root)
            parent_dest = ue_path_to_import_dest(parent_path, self.content_root) if parent_path else None

            if not parent_dest:
                self.log(f"  SKIP (no parent): {os.path.basename(mi_path)}")
                continue

            # Check if MI already exists
            exists = self._run_eval(UECommandBuilder.asset_exists(dest))
            if exists.lower() in ('true', '1'):
                # MI exists: verify texture parameters match expected values, fix if needed
                tex_values = []
                for tp in mi['tex_params']:
                    if not tp['texture_path']:
                        continue
                    tex_dest = ue_path_to_import_dest(tp['texture_path'], self.content_root)
                    tex_values.append({
                        'param_name': tp['param_name'],
                        'texture_ue_path': tex_dest,
                    })

                self.log(f"  CHECK (exists): {os.path.basename(mi_path)}")
                cmd = UECommandBuilder.check_and_fix_mi(dest, tex_values)
                r = self._run_cmd(cmd, timeout=120)
                result_str = r.get('result_text', '')
                output_str = r.get('output_text', '')
                if result_str:
                    self.log(f"    {result_str}")
                else:
                    self.log(f"    {output_str[-500:]}")
                continue

            # Build texture values (param_name -> imported texture UE path)
            tex_values = []
            for tp in mi['tex_params']:
                if not tp['texture_path']:
                    continue  # MI doesn't override this param
                tex_dest = ue_path_to_import_dest(tp['texture_path'], self.content_root)
                tex_values.append({
                    'param_name': tp['param_name'],
                    'texture_ue_path': tex_dest,
                })

            # Build scalar values
            scalar_values = mi['scalar_params']

            self.log(f"  Creating: {os.path.basename(mi_path)} (parent: {os.path.basename(parent_dest)})")
            self.log(f"    {len(tex_values)} texture overrides, {len(scalar_values)} scalar overrides")

            cmd = UECommandBuilder.create_material_instance(
                dest, parent_dest, tex_values, scalar_values
            )
            r = self._run_cmd(cmd, timeout=120)
            result_str = r.get('result_text', '')
            self.log(f"    {result_str[:200] or r.get('output_text','')[:200]}")

    def _step6_assign_materials(self, mesh_json_path, sections):
        """Step 6: Assign materials to mesh sections."""
        self.log(f"\n=== Step 6: Assigning materials to mesh sections ===")

        # Derive mesh UE path
        mesh_data = load_json(mesh_json_path)
        entries = mesh_data if isinstance(mesh_data, list) else [mesh_data]
        mesh_ue_path = None
        for e in entries:
            if isinstance(e, dict) and e.get('Type') == 'StaticMesh':
                pkg = e.get('Package', '')
                if pkg:
                    mesh_ue_path = pkg
                    break
        if not mesh_ue_path:
            rel = os.path.relpath(mesh_json_path, self.fmodel_root).replace('\\', '/')
            mesh_ue_path = '/Game/' + rel.replace('.json', '')

        mesh_dest = ue_path_to_import_dest(mesh_ue_path, self.content_root)

        # Build section -> material dest mapping
        assign_sections = []
        for i, s in enumerate(sections):
            mp = s['material_path']
            if mp.startswith('/Engine/') or mp.startswith('/Script/'):
                # Engine built-in material — use original path, no import needed
                mat_dest = mp
                self.log(f"  Slot {i} [{s['slot_name']}]: {os.path.basename(mp)} (engine builtin)")
            else:
                mat_dest = ue_path_to_import_dest(mp, self.content_root)
                self.log(f"  Slot {i} [{s['slot_name']}]: {os.path.basename(mat_dest)}")
            assign_sections.append({
                'slot_index': i,
                'material_dest_path': mat_dest,
            })

        cmd = UECommandBuilder.assign_materials(mesh_dest, assign_sections)
        r = self._run_cmd(cmd, timeout=120)
        result_str = r.get('result_text', '')
        self.log(f"  {result_str[:200] or r.get('output_text','')[:200]}")


# ======================== CLI Entry Point ========================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='UE Static Mesh Importer V2')
    parser.add_argument('mesh_json', help='Path to the mesh JSON file')
    parser.add_argument('--fmodel-root', default=r'I:\FModelOutput\Exports\SLASHER\Content',
                        help='FModel export root directory')
    parser.add_argument('--content-root', default='/Game/Developers/bosonhuang/SlasherAsset',
                        help='UE content root for import destination')
    parser.add_argument('--ue-host', default='239.0.0.1', help='UE multicast host')
    parser.add_argument('--ue-port', type=int, default=6766, help='UE multicast port')
    args = parser.parse_args()

    pipeline = MeshImportPipeline(
        fmodel_root=args.fmodel_root,
        content_root=args.content_root,
        ue_host=args.ue_host,
        ue_port=args.ue_port,
    )
    success = pipeline.run(args.mesh_json)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
