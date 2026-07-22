#!/usr/bin/env python3
"""UE Static Mesh Importer - View references and import meshes + dependencies to UE via Python Remote Execution.

Uses the asset_references.db built by ref_viewer.py to resolve a StaticMesh's material/texture dependencies.
Imports the .glb mesh, textures (.png), and builds simple materials with textures connected.
Then assigns materials to mesh sections based on the original StaticMaterials data.
"""

import os, sys, json, sqlite3, threading, queue, re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from collections import OrderedDict

# Ensure we can import sibling modules
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
DB_PATH = os.path.join(SCRIPT_DIR, "asset_references.db")

from ue_remote import UERemoteExec, test_connection

# ======================== Constants ========================

TYPE_COLORS = {
    'StaticMesh': '#4A90D9', 'Texture2D': '#7EC850', 'Material': '#E85A35',
    'MaterialInstanceConstant': '#E8A735',
}

# Suffix -> UE texture compression hint
TEXTURE_ROLE_HINTS = {
    '_N': 'Normalmap',
    '_ORM': 'Masks',
    '_M': 'Masks',
    '_C': 'Default',
    '_A': 'Default',
}

# Texture parameter name patterns in material JSON -> role
# Based on observed material JSON structure
TEX_PARAM_ROLES = {
    'PM_Diffuse': 'diffuse', 'Base_Albedo': 'diffuse', 'Albedo': 'diffuse',
    'PM_Normals': 'normal', 'Normals': 'normal', 'Normal': 'normal',
    'PM_SpecularMasks': 'orm', 'ORM': 'orm',
    'Masks': 'mask',
    'Convex_Convace_Thickness': 'mask',
}


# ======================== Database Helper ========================

class DBHelper:
    """Read-only database helper that reuses the schema from ref_viewer.py."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_meta(self, key):
        r = self.conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return r[0] if r else None

    def search_static_meshes(self, query=''):
        q = f"%{query}%" if query else "%"
        return [dict(r) for r in self.conn.execute(
            'SELECT * FROM assets WHERE type=? AND name LIKE ? ORDER BY name',
            ('StaticMesh', q))]

    def search_all_assets(self, query=''):
        q = f"%{query}%" if query else "%"
        return [dict(r) for r in self.conn.execute(
            'SELECT * FROM assets WHERE name LIKE ? ORDER BY type, name', (q,))]

    def get_by_path(self, pp):
        r = self.conn.execute('SELECT * FROM assets WHERE package_path=?', (pp,)).fetchone()
        return dict(r) if r else None

    def get_outgoing(self, aid):
        return [dict(r) for r in self.conn.execute(
            'SELECT r.target_path, r.target_type, a.id AS tid, a.name, a.type, a.file_path '
            'FROM refs r LEFT JOIN assets a ON r.target_path=a.package_path WHERE r.source_id=?', (aid,))]

    def get_outgoing_recursive(self, aid, max_depth=3):
        """Recursively gather all outgoing references. Returns dict {target_path: info}."""
        visited = {}
        self._recurse_outgoing(aid, visited, 0, max_depth)
        return visited

    def _recurse_outgoing(self, aid, visited, depth, max_depth):
        if depth >= max_depth:
            return
        refs = self.get_outgoing(aid)
        for r in refs:
            tp = r['target_path']
            if tp in visited:
                continue
            # Skip engine/script refs
            if tp.startswith('/Script/') or tp.startswith('/Engine/'):
                continue
            visited[tp] = r
            if r.get('tid'):
                self._recurse_outgoing(r['tid'], visited, depth + 1, max_depth)


# ======================== Path & File Utilities ========================

def norm_path(p):
    if not p:
        return p
    ls, ld = p.rfind('/'), p.rfind('.')
    return p[:ld] if ld > ls else p


def get_fmodel_roots(fmodel_root):
    """Get all possible root directories to search for files.
    FModel exports have two parallel structures:
    - Exports/SLASHER/Content/... : JSON metadata files
    - SLASHER/Content/...         : actual asset files (.glb, .png)
    We also support the case where everything is under one root.
    """
    roots = [fmodel_root]
    # If fmodel_root ends with Exports/xxx/Content, also check parent/xxx/Content
    norm = fmodel_root.replace('\\', '/')
    if '/Exports/' in norm:
        alt = norm.split('/Exports/')[0] + '/' + norm.split('/Exports/', 1)[1]
        roots.append(alt)
    # Also check if there's a sibling without Exports prefix
    parent = os.path.dirname(fmodel_root)
    if parent and os.path.basename(parent) == 'Exports':
        gp = os.path.dirname(parent)
        # grandparent + same subpath after Exports
        rel = os.path.relpath(fmodel_root, parent)
        alt2 = os.path.join(gp, rel)
        roots.append(alt2)
    return roots


def ue_path_to_local(ue_path, fmodel_root):
    """Convert /Game/xxx/Asset to local file paths under fmodel_root.
    Searches all possible root directories. Returns (json_path, glb_path) that exist."""
    rel = ue_path.replace('/Game/', '', 1) if ue_path.startswith('/Game/') else ue_path
    json_path = None
    glb_path = None
    for root in get_fmodel_roots(fmodel_root):
        jp = os.path.join(root, rel + '.json')
        gp = os.path.join(root, rel + '.glb')
        if os.path.isfile(jp) and not json_path:
            json_path = jp
        if os.path.isfile(gp) and not glb_path:
            glb_path = gp
    return json_path, glb_path


def ue_path_to_import_dest(ue_path, content_root):
    """Convert /Game/xxx/Asset to destination UE content path.
    content_root is like /Game/Developers/bosonhuang/SlasherAsset"""
    rel = ue_path.replace('/Game/', '', 1) if ue_path.startswith('/Game/') else ue_path
    return content_root.rstrip('/') + '/' + rel


def find_texture_file(ue_path, fmodel_root):
    """Find the .png texture file for a given UE texture path. Searches all roots."""
    rel = ue_path.replace('/Game/', '', 1) if ue_path.startswith('/Game/') else ue_path
    for root in get_fmodel_roots(fmodel_root):
        png_path = os.path.join(root, rel + '.png')
        if os.path.isfile(png_path):
            return png_path
    return None


def find_material_json(ue_path, fmodel_root):
    """Find the material JSON file for a given UE material path.
    Prefers the simplified format (dict with Textures/Parameters keys) over
    the FModel raw export format (list with Properties/TextureParameterValues)."""
    rel = ue_path.replace('/Game/', '', 1) if ue_path.startswith('/Game/') else ue_path
    candidates = []
    for root in get_fmodel_roots(fmodel_root):
        json_path = os.path.join(root, rel + '.json')
        if os.path.isfile(json_path):
            candidates.append(json_path)
    if not candidates:
        return None
    # Prefer the one with simplified format
    for jp in candidates:
        try:
            with open(jp, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            if isinstance(data, dict) and 'Textures' in data:
                return jp
        except Exception:
            pass
    return candidates[0]


def guess_texture_role(name):
    """Guess texture role from filename suffix."""
    n = name.upper()
    for suffix, role in [('_ORM', 'orm'), ('_N', 'normal'), ('_M', 'mask'),
                          ('_C', 'diffuse'), ('_A', 'diffuse')]:
        if n.endswith(suffix):
            return role
    # Check for common keywords
    if 'NORMAL' in n or 'NORM' in n:
        return 'normal'
    if 'ORM' in n or 'ROUGH' in n:
        return 'orm'
    if 'MASK' in n:
        return 'mask'
    return 'diffuse'


def type_color(t):
    if not t:
        return '#999999'
    for k, v in TYPE_COLORS.items():
        if k in t:
            return v
    return '#999999'


# ======================== Reference Resolver ========================

class RefResolver:
    """Resolves all dependencies for a StaticMesh: materials, textures, etc."""

    def __init__(self, db, fmodel_root):
        self.db = db
        self.fmodel_root = fmodel_root

    def resolve_mesh(self, mesh_path):
        """Resolve all dependencies for a StaticMesh.
        Returns dict with mesh info, sections (materials), and all texture files."""
        result = {
            'mesh_path': mesh_path,
            'mesh_name': os.path.basename(mesh_path),
            'glb_file': None,
            'json_file': None,
            'sections': [],      # [{slot_name, material_path, material_type, material_json, textures: {role: {ue_path, local_file}}}]
            'all_textures': [],  # [{ue_path, local_file, name, role}]
            'all_materials': [],  # [{ue_path, type, json_file, name}]
            'missing': [],       # [{ue_path, type, reason}]
        }

        # Find the asset in DB
        asset = self.db.get_by_path(mesh_path)
        if not asset:
            result['missing'].append({'ue_path': mesh_path, 'type': 'StaticMesh', 'reason': 'Not in database'})
            return result

        result['mesh_name'] = asset['name']

        # Find GLB file
        json_path, glb_path = ue_path_to_local(mesh_path, self.fmodel_root)
        result['json_file'] = json_path if os.path.isfile(json_path) else None
        result['glb_file'] = glb_path if os.path.isfile(glb_path) else None
        if not result['glb_file']:
            result['missing'].append({'ue_path': mesh_path, 'type': 'StaticMesh', 'reason': 'GLB file not found'})

        # Read the StaticMesh JSON to get StaticMaterials
        mesh_json = None
        if result['json_file']:
            try:
                with open(result['json_file'], 'r', encoding='utf-8-sig') as f:
                    mesh_json = json.load(f)
            except Exception:
                pass

        # Parse StaticMaterials from JSON
        static_materials = []
        if mesh_json:
            entries = mesh_json if isinstance(mesh_json, list) else [mesh_json]
            for e in entries:
                if isinstance(e, dict) and e.get('Type') == 'StaticMesh':
                    sms = e.get('Properties', {}).get('StaticMaterials', [])
                    for sm in sms:
                        mi = sm.get('MaterialInterface', {})
                        obj_name = mi.get('ObjectName', '')
                        obj_path = mi.get('ObjectPath', '')
                        if obj_path:
                            mat_path = norm_path('/' + obj_path.rsplit('.', 1)[0]) if '.' in obj_path else obj_path
                            # Actually ObjectPath is like /Game/.../Asset.0
                            mat_path = norm_path(obj_path)
                            mat_type = 'Material'
                            if 'MaterialInstanceConstant' in obj_name:
                                mat_type = 'MaterialInstanceConstant'
                            elif 'Material' in obj_name:
                                mat_type = 'Material'
                            static_materials.append({
                                'slot_name': sm.get('MaterialSlotName', f'Slot_{len(static_materials)}'),
                                'material_path': mat_path,
                                'material_type': mat_type,
                            })
                    break

        # If no JSON data, use DB refs
        if not static_materials:
            refs = self.db.get_outgoing(asset['id'])
            for r in refs:
                rtype = r.get('target_type') or r.get('type') or ''
                if 'Material' in rtype or 'MaterialInstance' in rtype:
                    static_materials.append({
                        'slot_name': f'Slot_{len(static_materials)}',
                        'material_path': r['target_path'],
                        'material_type': rtype,
                    })

        # Resolve each material and its textures
        seen_materials = {}
        seen_textures = {}

        for sm in static_materials:
            mat_path = sm['material_path']
            section = {
                'slot_name': sm['slot_name'],
                'material_path': mat_path,
                'material_type': sm['material_type'],
                'material_json': None,
                'textures': {},  # role -> {ue_path, local_file, name}
                'found': False,
            }

            # Find material JSON
            mat_json_file = find_material_json(mat_path, self.fmodel_root)
            if mat_json_file:
                section['material_json'] = mat_json_file
                section['found'] = True
                if mat_path not in seen_materials:
                    mat_info = {
                        'ue_path': mat_path,
                        'type': sm['material_type'],
                        'json_file': mat_json_file,
                        'name': os.path.basename(mat_path),
                    }
                    result['all_materials'].append(mat_info)
                    seen_materials[mat_path] = mat_info

                # Parse textures from material JSON
                try:
                    with open(mat_json_file, 'r', encoding='utf-8-sig') as f:
                        mat_data = json.load(f)
                    # Support two formats:
                    # 1. Simplified: { "Textures": { "param": "/Game/path.Asset" } }
                    # 2. FModel raw: [ { "Properties": { "TextureParameterValues": [...] } } ]
                    textures = {}
                    if isinstance(mat_data, dict) and 'Textures' in mat_data:
                        textures = mat_data['Textures']
                    elif isinstance(mat_data, list):
                        for e in mat_data:
                            if isinstance(e, dict) and 'Properties' in e:
                                tpv = e['Properties'].get('TextureParameterValues', [])
                                for tp in tpv:
                                    if isinstance(tp, dict):
                                        param_info = tp.get('ParameterInfo', {})
                                        param_name = param_info.get('Name', '') if isinstance(param_info, dict) else ''
                                        val = tp.get('ParameterValue', {})
                                        if isinstance(val, dict):
                                            tex_path = val.get('ObjectName', '') or val.get('ObjectPath', '')
                                            # ObjectName is like "Texture2D'T_Atlass_A_C'"
                                            # ObjectPath is like "/Game/.../T_Atlass_A_C.T_Atlass_A_C"
                                            if tex_path and tex_path.startswith('/Game/'):
                                                tex_path = norm_path(tex_path)
                                            elif tex_path and "'" in tex_path:
                                                # Extract from "Texture2D'T_Atlass_A_C'" - need to find actual path
                                                pass
                                            if param_name and tex_path and tex_path.startswith('/Game/'):
                                                textures[param_name] = tex_path
                                break
                    for param_name, tex_path in textures.items():
                        if not isinstance(tex_path, str) or not tex_path:
                            continue
                        tex_norm = norm_path(tex_path)
                        if tex_norm.startswith('/Script/') or tex_norm.startswith('/Engine/'):
                            continue
                        tex_local = find_texture_file(tex_norm, self.fmodel_root)
                        role = TEX_PARAM_ROLES.get(param_name, guess_texture_role(os.path.basename(tex_norm)))
                        if role not in section['textures']:  # first occurrence wins
                            section['textures'][role] = {
                                'ue_path': tex_norm,
                                'local_file': tex_local,
                                'name': os.path.basename(tex_norm),
                                'param_name': param_name,
                            }
                        if tex_norm not in seen_textures:
                            result['all_textures'].append({
                                'ue_path': tex_norm,
                                'local_file': tex_local,
                                'name': os.path.basename(tex_norm),
                                'role': role,
                            })
                            seen_textures[tex_norm] = True
                except Exception:
                    pass
            else:
                result['missing'].append({
                    'ue_path': mat_path,
                    'type': sm['material_type'],
                    'reason': 'Material JSON not found'
                })

            result['sections'].append(section)

        return result


# ======================== UE Import Command Builder ========================

class UEImportBuilder:
    """Builds Python command strings for UE Remote Execution."""

    @staticmethod
    def import_texture(local_file, dest_ue_path, role='diffuse'):
        """Build command to import a texture into UE."""
        name = os.path.basename(dest_ue_path)
        dest_dir = os.path.dirname(dest_ue_path)
        # Use forward slashes for UE paths
        dest_dir = dest_dir.replace('\\', '/')

        local_file_fwd = local_file.replace('\\', '/')

        # Map role to UE texture settings
        if role == 'normal':
            compression_settings = 'unreal.TextureCompressionSettings.TC_Normalmap'
            srgb = 'False'
        elif role == 'orm':
            compression_settings = 'unreal.TextureCompressionSettings.TC_Masks'
            srgb = 'False'
        elif role == 'mask':
            compression_settings = 'unreal.TextureCompressionSettings.TC_Masks'
            srgb = 'False'
        else:
            compression_settings = 'unreal.TextureCompressionSettings.TC_Default'
            srgb = 'True'

        cmd = f'''
import unreal
task = unreal.AssetImportTask()
task.set_editor_property("automated", True)
task.set_editor_property("filename", r"{local_file_fwd}")
task.set_editor_property("destination_path", "{dest_dir}")
task.set_editor_property("destination_name", "{name}")
task.set_editor_property("replace_existing", True)
task.set_editor_property("save", True)
unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

# Load the imported texture and configure its settings
tex_path = "{dest_dir}/{name}"
texture = unreal.load_asset(tex_path)
if texture and isinstance(texture, unreal.Texture):
    texture.set_editor_property("compression_settings", {compression_settings})
    texture.set_editor_property("srgb", {srgb})
    unreal.TextureEditingModule.update_texture(texture)
    unreal.EditorAssetLibrary.save_asset(tex_path)
    result = f"Imported texture: {{texture.get_name()}} (role={role})"
else:
    # Check if a non-texture asset was created (wrong type)
    all_assets = unreal.EditorAssetLibrary.list_assets("{dest_dir}", recursive=False)
    found = None
    for ap in all_assets:
        ao = unreal.load_asset(ap)
        if ao and isinstance(ao, unreal.Texture):
            found = ao
            break
    if found:
        found.set_editor_property("compression_settings", {compression_settings})
        found.set_editor_property("srgb", {srgb})
        unreal.TextureEditingModule.update_texture(found)
        unreal.EditorAssetLibrary.save_asset(found.get_path_name())
        result = f"Imported texture (renamed): {{found.get_name()}}"
    else:
        result = "ERROR: Texture import failed"
'''
        return cmd.strip()

    @staticmethod
    def fix_texture_settings(dest_ue_path, role='diffuse'):
        """Build command to fix texture settings for an already-imported texture."""
        if role == 'normal':
            compression_settings = 'unreal.TextureCompressionSettings.TC_Normalmap'
            srgb = 'False'
        elif role in ('orm', 'mask'):
            compression_settings = 'unreal.TextureCompressionSettings.TC_Masks'
            srgb = 'False'
        else:
            compression_settings = 'unreal.TextureCompressionSettings.TC_Default'
            srgb = 'True'

        cmd = f'''
import unreal
texture = unreal.load_asset("{dest_ue_path}")
if texture and isinstance(texture, unreal.Texture):
    texture.set_editor_property("compression_settings", {compression_settings})
    texture.set_editor_property("srgb", {srgb})
    unreal.TextureEditingModule.update_texture(texture)
    unreal.EditorAssetLibrary.save_asset("{dest_ue_path}")
    result = f"Fixed texture settings: {{texture.get_name()}}"
else:
    result = "ERROR: Texture not found"
'''
        return cmd.strip()

    @staticmethod
    def import_mesh(local_file, dest_ue_path):
        """Build command to import a GLB mesh into UE."""
        name = os.path.basename(dest_ue_path)
        dest_dir = os.path.dirname(dest_ue_path).replace('\\', '/')

        local_file_fwd = local_file.replace('\\', '/')

        cmd = f'''
import unreal
task = unreal.AssetImportTask()
task.set_editor_property("automated", True)
task.set_editor_property("filename", r"{local_file_fwd}")
task.set_editor_property("destination_path", "{dest_dir}")
task.set_editor_property("destination_name", "{name}")
task.set_editor_property("replace_existing", True)
task.set_editor_property("save", True)
unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

# GLB importer may use internal mesh name instead of destination_name
# Check if the expected asset exists, if not, find and rename it
expected_path = "{dest_dir}/{name}"
imported = unreal.load_asset(expected_path)
if not imported:
    all_assets = unreal.EditorAssetLibrary.list_assets("{dest_dir}", recursive=False)
    for asset_path in all_assets:
        asset_obj = unreal.load_asset(asset_path)
        if asset_obj and isinstance(asset_obj, unreal.StaticMesh):
            if asset_path != expected_path:
                unreal.EditorAssetLibrary.rename_asset(asset_path, expected_path)
                imported = unreal.load_asset(expected_path)
            else:
                imported = asset_obj
            break
result = [str(imported)] if imported else []
'''
        return cmd.strip()

    @staticmethod
    def create_material(dest_ue_path, tex_assignments, material_name=None):
        """Build command to create a simple material with textures connected.
        tex_assignments: list of {ue_path, role, param_name}
        """
        name = material_name or os.path.basename(dest_ue_path)
        dest_dir = os.path.dirname(dest_ue_path).replace('\\', '/')

        # Build texture sampler setup commands
        tex_cmds = []
        for ta in tex_assignments:
            role = ta.get('role', 'diffuse')
            tex_path = ta['ue_path']
            tex_obj = tex_path + '.' + os.path.basename(tex_path)
            if role == 'diffuse':
                tex_cmds.append(f'''
try:
    tex_{role} = unreal.load_asset("{tex_obj}")
    if tex_{role}:
        ts_{role} = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionTextureSample, -400, 0)
        ts_{role}.set_editor_property("texture", tex_{role})
        unreal.MaterialEditingLibrary.connect_material_property(ts_{role}, "RGB", unreal.MaterialProperty.MP_BASE_COLOR)
except Exception as e:
    print(f"Diffuse tex error: {{e}}")
''')
            elif role == 'normal':
                tex_cmds.append(f'''
try:
    tex_{role} = unreal.load_asset("{tex_obj}")
    if tex_{role}:
        ts_{role} = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionTextureSample, -400, 200)
        ts_{role}.set_editor_property("texture", tex_{role})
        ts_{role}.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
        unreal.MaterialEditingLibrary.connect_material_property(ts_{role}, "RGB", unreal.MaterialProperty.MP_NORMAL)
except Exception as e:
    print(f"Normal tex error: {{e}}")
''')
            elif role == 'orm':
                tex_cmds.append(f'''
try:
    tex_{role} = unreal.load_asset("{tex_obj}")
    if tex_{role}:
        ts_{role} = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionTextureSample, -400, 400)
        ts_{role}.set_editor_property("texture", tex_{role})
        ts_{role}.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
        unreal.MaterialEditingLibrary.connect_material_property(ts_{role}, "R", unreal.MaterialProperty.MP_AMBIENT_OCCLUSION)
        unreal.MaterialEditingLibrary.connect_material_property(ts_{role}, "G", unreal.MaterialProperty.MP_ROUGHNESS)
        unreal.MaterialEditingLibrary.connect_material_property(ts_{role}, "B", unreal.MaterialProperty.MP_METALLIC)
except Exception as e:
    print(f"ORM tex error: {{e}}")
''')
            elif role == 'mask':
                tex_cmds.append(f'''
try:
    tex_{role} = unreal.load_asset("{tex_obj}")
    if tex_{role}:
        ts_{role} = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionTextureSample, -400, 600)
        ts_{role}.set_editor_property("texture", tex_{role})
except Exception as e:
    print(f"Mask tex error: {{e}}")
''')

        # Indent each line by 4 spaces so tex_block stays inside the else: block
        raw_block = '\n'.join(tex_cmds)
        if raw_block.strip():
            tex_block = '\n'.join(('    ' + line) if line.strip() else line for line in raw_block.split('\n'))
        else:
            tex_block = '    pass  # no textures to connect'

        cmd = f'''
import unreal
# Delete existing material if present
existing = unreal.EditorAssetLibrary.find_asset_data("{dest_ue_path}")
if existing:
    unreal.EditorAssetLibrary.delete_asset("{dest_ue_path}")

# Create new material using AssetTools + MaterialFactoryNew
asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
mat = asset_tools.create_asset("{name}", "{dest_dir}", unreal.Material, unreal.MaterialFactoryNew())
if not mat:
    result = "ERROR: Failed to create material"
else:
    # Create default root nodes for fallback values
    root_diffuse = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionConstant3Vector, -800, 0)
    root_diffuse.set_editor_property("Constant", [0.5, 0.5, 0.5])

    root_normal_r = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionConstant, -800, 200)
    root_normal_r.set_editor_property("R", 0.0)
    root_normal_g = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionConstant, -800, 250)
    root_normal_g.set_editor_property("R", 0.0)

    root_roughness = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionConstant, -800, 400)
    root_roughness.set_editor_property("R", 0.5)

    root_metallic = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionConstant, -800, 500)
    root_metallic.set_editor_property("R", 0.0)

    # Connect defaults first
    unreal.MaterialEditingLibrary.connect_material_property(root_diffuse, "RGB", unreal.MaterialProperty.MP_BASE_COLOR)
    unreal.MaterialEditingLibrary.connect_material_property(root_normal_r, "", unreal.MaterialProperty.MP_NORMAL)
    unreal.MaterialEditingLibrary.connect_material_property(root_normal_g, "", unreal.MaterialProperty.MP_NORMAL)
    unreal.MaterialEditingLibrary.connect_material_property(root_roughness, "", unreal.MaterialProperty.MP_ROUGHNESS)
    unreal.MaterialEditingLibrary.connect_material_property(root_metallic, "", unreal.MaterialProperty.MP_METALLIC)

    # Now connect textures (overrides defaults)
{tex_block}

    # Recompile and save
    unreal.MaterialEditingLibrary.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset("{dest_ue_path}")
    result = f"Created material: {{mat.get_name()}}"
'''
        return cmd.strip()

    @staticmethod
    def assign_material_to_mesh(mesh_ue_path, sections):
        """Build command to assign materials to a static mesh's sections.
        sections: list of {slot_name, material_dest_path, found}
        """
        slot_assignments = []
        for i, s in enumerate(sections):
            mat_path = s.get('material_dest_path', '')
            slot_name = s.get('slot_name', f'Slot_{i}')
            if mat_path:
                slot_assignments.append((i, slot_name, mat_path))

        lines = []
        for idx, sname, mpath in slot_assignments:
            lines.append(f'''
try:
    mat_{idx} = unreal.load_asset("{mpath}")
    if mat_{idx}:
        mesh.set_material({idx}, mat_{idx})
except Exception as e:
    print(f"Slot {idx} assign error: {{e}}")
''')

        # Indent assign_block by 4 spaces for the else: block
        raw_assign = '\n'.join(lines)
        if raw_assign.strip():
            assign_block = '\n'.join(('    ' + line) if line.strip() else line for line in raw_assign.split('\n'))
        else:
            assign_block = '    pass'

        cmd = f'''
import unreal
mesh = unreal.load_asset("{mesh_ue_path}")
if not mesh or not isinstance(mesh, unreal.StaticMesh):
    result = f"ERROR: Mesh not found or not StaticMesh: {mesh_ue_path}"
else:
{assign_block}

    # Save
    unreal.EditorAssetLibrary.save_asset("{mesh_ue_path}")
    result = f"Assigned materials to {{mesh.get_name()}}"
'''
        return cmd.strip()

    @staticmethod
    def ensure_directory(ue_dir_path):
        """Ensure a UE content directory exists."""
        return f'''
import unreal
unreal.EditorAssetLibrary.make_directory("{ue_dir_path}")
result = "ok"
'''.strip()


# ======================== GUI Application ========================

class MeshImporterApp:
    def __init__(self, root):
        self.root = root
        self.db = DBHelper(DB_PATH) if os.path.isfile(DB_PATH) else None
        self.queue = queue.Queue()
        self.importing = False
        self.ue_client = None
        self.fmodel_root = self.db.get_meta('last_dir') if self.db else r"I:\FModelOutput\Exports\SLASHER\Content"
        self.content_root = "/Game/Developers/bosonhuang/SlasherAsset"
        self.ue_host = "239.0.0.1"
        self.ue_port = 6766
        self.current_resolution = None
        self.tree_map = {}

        root.title("UE Static Mesh Importer")
        root.geometry("1400x860")
        root.minsize(1000, 640)
        self._build_ui()
        self._refresh_tree()
        self._poll()

    def _build_ui(self):
        # Top: settings bar
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill='x')

        ttk.Label(top, text="FModel Root:").grid(row=0, column=0, sticky='w')
        self.fmodel_var = tk.StringVar(value=self.fmodel_root)
        ttk.Entry(top, textvariable=self.fmodel_var, width=50).grid(row=0, column=1, sticky='we', padx=4)
        ttk.Button(top, text="Browse", command=self._browse_fmodel).grid(row=0, column=2)

        ttk.Label(top, text="UE Content Root:").grid(row=1, column=0, sticky='w')
        self.content_var = tk.StringVar(value=self.content_root)
        ttk.Entry(top, textvariable=self.content_var, width=50).grid(row=1, column=1, sticky='we', padx=4)

        ttk.Label(top, text="UE Host:Port:").grid(row=2, column=0, sticky='w')
        self.host_var = tk.StringVar(value=self.ue_host)
        self.port_var = tk.StringVar(value=str(self.ue_port))
        hp_frame = ttk.Frame(top)
        hp_frame.grid(row=2, column=1, sticky='w')
        ttk.Entry(hp_frame, textvariable=self.host_var, width=15).pack(side='left')
        ttk.Label(hp_frame, text=":").pack(side='left')
        ttk.Entry(hp_frame, textvariable=self.port_var, width=6).pack(side='left')
        ttk.Button(hp_frame, text="Test Connection", command=self._test_ue).pack(side='left', padx=8)

        top.columnconfigure(1, weight=1)

        # Main area: left tree, center details, right log
        main = ttk.Frame(self.root)
        main.pack(fill='both', expand=True, padx=8, pady=4)

        # Left: asset search + tree
        left = ttk.Frame(main, width=320)
        left.pack(side='left', fill='y')
        left.pack_propagate(False)

        ttk.Label(left, text="Search StaticMesh:").pack(anchor='w')
        self.search_var = tk.StringVar()
        self.search_var.trace('w', lambda *_: self._filter_tree())
        ttk.Entry(left, textvariable=self.search_var).pack(fill='x', pady=(0, 4))

        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=('type',), show='tree headings')
        self.tree.heading('#0', text='Asset')
        self.tree.heading('type', text='Type')
        self.tree.column('#0', width=200)
        self.tree.column('type', width=70)
        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        self.tree.bind('<Double-1>', self._on_tree_dblclick)

        # Center: reference details
        center = ttk.Frame(main)
        center.pack(side='left', fill='both', expand=True, padx=4)

        ttk.Label(center, text="Reference Details", font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        detail_frame = ttk.Frame(center)
        detail_frame.pack(fill='both', expand=True)

        self.detail_text = tk.Text(detail_frame, wrap='word', state='disabled',
                                   bg='#1E1E1E', fg='#DDDDDD', font=('Consolas', 9),
                                   insertbackground='white')
        dsb = ttk.Scrollbar(detail_frame, orient='vertical', command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=dsb.set)
        self.detail_text.pack(side='left', fill='both', expand=True)
        dsb.pack(side='right', fill='y')

        # Buttons
        btn_frame = ttk.Frame(center)
        btn_frame.pack(fill='x', pady=4)
        self.resolve_btn = ttk.Button(btn_frame, text="Resolve References", command=self._resolve_selected)
        self.resolve_btn.pack(side='left', padx=4)
        self.import_btn = ttk.Button(btn_frame, text="Import to UE", command=self._start_import, state='disabled')
        self.import_btn.pack(side='left', padx=4)

        # Right: log
        right = ttk.Frame(main, width=400)
        right.pack(side='right', fill='y')
        right.pack_propagate(False)

        ttk.Label(right, text="Import Log", font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        log_frame = ttk.Frame(right)
        log_frame.pack(fill='both', expand=True)
        self.log_text = tk.Text(log_frame, wrap='word', state='disabled',
                                bg='#1E1E1E', fg='#88FF88', font=('Consolas', 9))
        lsb = ttk.Scrollbar(log_frame, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        lsb.pack(side='right', fill='y')

        # Progress bar
        self.pb = ttk.Progressbar(self.root, mode='determinate')
        self.pb.pack(fill='x', padx=8, pady=2)
        self.status_var = tk.StringVar(value="Ready. Select a StaticMesh to view its references.")
        ttk.Label(self.root, textvariable=self.status_var).pack(anchor='w', padx=8)

    def _browse_fmodel(self):
        d = filedialog.askdirectory(initialdir=self.fmodel_var.get())
        if d:
            self.fmodel_var.set(d)
            self.fmodel_root = d

    def _test_ue(self):
        self._log("Testing UE connection...")
        ok, msg = test_connection(self.host_var.get(), int(self.port_var.get()))
        if ok:
            self._log(f"✓ {msg}")
            messagebox.showinfo("Connection", msg)
        else:
            # msg may contain multi-line debug log; log each line
            for line in msg.split('\n'):
                self._log(line)
            messagebox.showwarning("Connection Failed", "See log panel for details.")

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.tree_map.clear()
        if not self.db:
            return
        assets = self.db.search_static_meshes('')
        for a in assets:
            item = self.tree.insert('', 'end', text=a['name'], values=(a['type'],))
            self.tree_map[item] = a['package_path']

    def _filter_tree(self):
        q = self.search_var.get()
        self.tree.delete(*self.tree.get_children())
        self.tree_map.clear()
        if not self.db:
            return
        assets = self.db.search_static_meshes(q)
        for a in assets:
            item = self.tree.insert('', 'end', text=a['name'], values=(a['type'],))
            self.tree_map[item] = a['package_path']

    def _on_tree_select(self, e):
        sel = self.tree.selection()
        if not sel:
            return
        pp = self.tree_map.get(sel[0])
        if pp:
            self._show_asset_info(pp)

    def _on_tree_dblclick(self, e):
        self._resolve_selected()

    def _show_asset_info(self, mesh_path):
        self._clear_detail()
        asset = self.db.get_by_path(mesh_path) if self.db else None
        if not asset:
            self._detail_append(f"Path: {mesh_path}\n(Not in database)\n")
            return
        self._detail_append(f"Name:   {asset['name']}\n")
        self._detail_append(f"Type:   {asset['type']}\n")
        self._detail_append(f"Path:   {asset['package_path']}\n")
        self._detail_append(f"File:   {asset['file_path']}\n")
        self._detail_append(f"\n--- Click 'Resolve References' to analyze dependencies ---\n")

    def _resolve_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        pp = self.tree_map.get(sel[0])
        if not pp:
            return
        self.fmodel_root = self.fmodel_var.get()
        self._log(f"Resolving references for: {pp}")
        resolver = RefResolver(self.db, self.fmodel_root)
        self.current_resolution = resolver.resolve_mesh(pp)
        self._display_resolution(self.current_resolution)
        self.import_btn.config(state='normal')
        self._log(f"  Found {len(self.current_resolution['sections'])} sections, "
                  f"{len(self.current_resolution['all_textures'])} textures, "
                  f"{len(self.current_resolution['all_materials'])} materials")
        if self.current_resolution['missing']:
            self._log(f"  ⚠ {len(self.current_resolution['missing'])} missing items")

    def _display_resolution(self, res):
        self._clear_detail()
        self._detail_append(f"=== StaticMesh: {res['mesh_name']} ===\n\n")
        self._detail_append(f"GLB File: {res['glb_file'] or 'NOT FOUND'}\n")
        self._detail_append(f"JSON File: {res['json_file'] or 'NOT FOUND'}\n\n")

        self._detail_append(f"--- Sections ({len(res['sections'])}) ---\n")
        for i, s in enumerate(res['sections']):
            found_str = "✓" if s['found'] else "✗ MISSING"
            self._detail_append(f"\n  [{i}] Slot: {s['slot_name']}\n")
            self._detail_append(f"      Material: {s['material_path']}\n")
            self._detail_append(f"      Type: {s['material_type']}  [{found_str}]\n")
            if s['textures']:
                for role, t in s['textures'].items():
                    found_t = "✓" if t['local_file'] else "✗"
                    self._detail_append(f"      Tex [{role}]: {t['name']}  [{found_t}]\n")
                    self._detail_append(f"        param: {t['param_name']}\n")
                    self._detail_append(f"        path: {t['ue_path']}\n")

        if res['all_textures']:
            self._detail_append(f"\n--- All Unique Textures ({len(res['all_textures'])}) ---\n")
            for t in res['all_textures']:
                found_t = "✓" if t['local_file'] else "✗"
                self._detail_append(f"  {t['name']} [{t['role']}] {found_t}\n")

        if res['all_materials']:
            self._detail_append(f"\n--- All Materials ({len(res['all_materials'])}) ---\n")
            for m in res['all_materials']:
                self._detail_append(f"  {m['name']} [{m['type']}]\n")

        if res['missing']:
            self._detail_append(f"\n--- Missing ({len(res['missing'])}) ---\n")
            for m in res['missing']:
                self._detail_append(f"  ⚠ {m['ue_path']} [{m['type']}]: {m['reason']}\n")

        self._detail_append(f"\n--- Ready to import. Click 'Import to UE' ---\n")

    def _start_import(self):
        if not self.current_resolution:
            return
        self.content_root = self.content_var.get()
        self.ue_host = self.host_var.get()
        self.ue_port = int(self.port_var.get())
        self.importing = True
        self.import_btn.config(state='disabled')
        self.resolve_btn.config(state='disabled')
        self.pb['value'] = 0
        t = threading.Thread(target=self._import_worker, daemon=True)
        t.start()

    def _import_worker(self):
        res = self.current_resolution
        try:
            # Step 1: Connect to UE
            self._log("Connecting to UE...")
            self.queue.put(('progress', 0, 10, "Connecting to UE..."))
            client = UERemoteExec(self.ue_host, self.ue_port)
            if not client.connect(timeout=10):
                self.queue.put(('error', "Cannot connect to UE. Ensure UE is running with Python Remote Execution enabled."))
                return
            self._log("✓ Connected to UE")

            # Collect all import tasks
            total_steps = 0
            total_steps += len(res['all_textures'])  # textures
            total_steps += len(res['all_materials'])  # materials
            total_steps += 1  # mesh
            total_steps += 1  # assign materials
            step = 0

            # Step 2: Import textures
            for t in res['all_textures']:
                if not t['local_file']:
                    self._log(f"  SKIP texture (no file): {t['name']}")
                    step += 1
                    continue
                dest = ue_path_to_import_dest(t['ue_path'], self.content_root)
                # Check if texture already exists in UE
                check_cmd = f'__import__("unreal").EditorAssetLibrary.does_asset_exist("{dest}")'
                r = client.run_command(check_cmd, mode='eval', timeout=30)
                exists = r.get('success') and str(r.get('result', '')).strip().lower() in ('true', '1')
                if exists:
                    # Fix texture settings even if already imported
                    fix_cmd = UEImportBuilder.fix_texture_settings(dest, t['role'])
                    client.run_command(fix_cmd, mode='exec', timeout=60)
                    self._log(f"  SKIP texture (exists, settings fixed): {t['name']}")
                    step += 1
                    self.queue.put(('progress', step, total_steps, f"Texture {step}/{len(res['all_textures'])}"))
                    continue
                self._log(f"  Importing texture: {t['name']} -> {dest}")
                cmd = UEImportBuilder.import_texture(t['local_file'], dest, t['role'])
                r = client.run_command(cmd, mode='exec', timeout=120)
                if not r.get('success'):
                    self._log(f"    ⚠ Texture import warning: {r.get('error', '')[:200]}")
                step += 1
                self.queue.put(('progress', step, total_steps, f"Texture {step}/{len(res['all_textures'])}"))

            # Step 3: Create materials
            for m in res['all_materials']:
                dest = ue_path_to_import_dest(m['ue_path'], self.content_root)
                # Find the section that uses this material to get texture assignments
                tex_assignments = []
                for s in res['sections']:
                    if s['material_path'] == m['ue_path']:
                        for role, t in s['textures'].items():
                            tex_assignments.append({
                                'ue_path': ue_path_to_import_dest(t['ue_path'], self.content_root),
                                'role': role,
                                'param_name': t['param_name'],
                            })
                        break
                self._log(f"  Creating material: {m['name']} -> {dest}")
                # If material is "IsNull" or empty, still create a simple default material
                cmd = UEImportBuilder.create_material(dest, tex_assignments)
                r = client.run_command(cmd, mode='exec', timeout=120)
                if not r.get('success'):
                    self._log(f"    ⚠ Material creation warning: {r.get('error', '')[:200]}")
                step += 1
                self.queue.put(('progress', step, total_steps, f"Material {step}"))

            # Step 4: Import mesh
            if res['glb_file']:
                dest = ue_path_to_import_dest(res['mesh_path'], self.content_root)
                self._log(f"  Importing mesh: {res['mesh_name']} -> {dest}")
                cmd = UEImportBuilder.import_mesh(res['glb_file'], dest)
                r = client.run_command(cmd, mode='exec', timeout=300)
                if not r.get('success'):
                    self._log(f"    ⚠ Mesh import warning: {r.get('error', '')[:200]}")
                else:
                    self._log(f"    ✓ Mesh imported")
                step += 1
                self.queue.put(('progress', step, total_steps, "Mesh imported"))
            else:
                self._log("  ⚠ No GLB file, skipping mesh import")

            # Step 5: Assign materials to mesh sections
            mesh_dest = ue_path_to_import_dest(res['mesh_path'], self.content_root)
            sections_for_assign = []
            for s in res['sections']:
                mat_dest = ue_path_to_import_dest(s['material_path'], self.content_root) if s['found'] else None
                sections_for_assign.append({
                    'slot_name': s['slot_name'],
                    'material_dest_path': mat_dest,
                    'found': s['found'],
                })
            self._log("  Assigning materials to mesh sections...")
            cmd = UEImportBuilder.assign_material_to_mesh(mesh_dest, sections_for_assign)
            r = client.run_command(cmd, mode='exec', timeout=120)
            if not r.get('success'):
                self._log(f"    ⚠ Assign warning: {r.get('error', '')[:200]}")
            else:
                self._log(f"    ✓ {r.get('result', 'Materials assigned')}")
            step += 1
            self.queue.put(('progress', step, total_steps, "Done!"))

            client.disconnect()
            self._log("✓ Import complete!")
            self.queue.put(('done',))

        except Exception as e:
            self.queue.put(('error', str(e)))

    def _poll(self):
        try:
            while True:
                m = self.queue.get_nowait()
                if m[0] == 'progress':
                    _, cur, total, msg = m
                    self.pb['maximum'] = total
                    self.pb['value'] = cur
                    self.status_var.set(msg)
                elif m[0] == 'done':
                    self.status_var.set("Import complete!")
                    self.importing = False
                    self.import_btn.config(state='normal')
                    self.resolve_btn.config(state='normal')
                elif m[0] == 'error':
                    self._log(f"✗ ERROR: {m[1]}")
                    self.status_var.set(f"Error: {m[1]}")
                    self.importing = False
                    self.import_btn.config(state='normal')
                    self.resolve_btn.config(state='normal')
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # --- UI helpers ---

    def _log(self, msg):
        self.log_text.config(state='normal')
        self.log_text.insert('end', msg + '\n')
        self.log_text.see('end')
        self.log_text.config(state='disabled')

    def _clear_detail(self):
        self.detail_text.config(state='normal')
        self.detail_text.delete('1.0', 'end')
        self.detail_text.config(state='disabled')

    def _detail_append(self, text):
        self.detail_text.config(state='normal')
        self.detail_text.insert('end', text)
        self.detail_text.see('end')
        self.detail_text.config(state='disabled')


# ======================== Entry Point ========================

def main():
    if not os.path.isfile(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        print("Please run ref_viewer.py first to build the database.")
        sys.exit(1)
    root = tk.Tk()
    MeshImporterApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()