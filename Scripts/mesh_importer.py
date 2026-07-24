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
    '_C': 'Masks',   # Cavity/Convex - linear data, not color
    '_A': 'Default', # Albedo - color
}

# Texture parameter name → full connection spec
# This is the primary source of truth for how textures connect to material outputs.
# Derived from Master Material parameter names observed in FModel exports.
PARAM_CONNECTIONS = {
    'Base_Albedo': {
        'role': 'diffuse', 'srgb': True,
        'compression': 'unreal.TextureCompressionSettings.TC_Default',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_BASE_COLOR')],
    },
    'Albedo': {
        'role': 'diffuse', 'srgb': True,
        'compression': 'unreal.TextureCompressionSettings.TC_Default',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_BASE_COLOR')],
    },
    'PM_Diffuse': {
        'role': 'diffuse', 'srgb': True,
        'compression': 'unreal.TextureCompressionSettings.TC_Default',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_BASE_COLOR')],
    },
    'Normals': {
        'role': 'normal', 'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_Normalmap',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_NORMAL')],
    },
    'Normal': {
        'role': 'normal', 'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_Normalmap',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_NORMAL')],
    },
    'PM_Normals': {
        'role': 'normal', 'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_Normalmap',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_NORMAL')],
    },
    'ORM': {
        'role': 'orm', 'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_Masks',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [
            ('R', 'unreal.MaterialProperty.MP_AMBIENT_OCCLUSION'),
            ('G', 'unreal.MaterialProperty.MP_ROUGHNESS'),
            ('B', 'unreal.MaterialProperty.MP_METALLIC'),
        ],
    },
    'PM_SpecularMasks': {
        'role': 'orm', 'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_Masks',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [
            ('R', 'unreal.MaterialProperty.MP_AMBIENT_OCCLUSION'),
            ('G', 'unreal.MaterialProperty.MP_ROUGHNESS'),
            ('B', 'unreal.MaterialProperty.MP_METALLIC'),
        ],
    },
    'Convex_Convace_Thickness': {
        'role': 'mask', 'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_Masks',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [],  # Cavity/curvature mask - not directly connected to a standard output
    },
    'Masks': {
        'role': 'mask', 'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_Masks',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [],  # Generic mask - not directly connected
    },
    'Mask': {
        'role': 'mask', 'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_Masks',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [],
    },
    'VirtualMask': {
        'role': 'virtual_mask', 'srgb': False,
        'compression': 'unreal.TextureCompressionSettings.TC_Masks',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_VIRTUAL_MASKS',
        'connections': [],
    },
    'VirtualColor': {
        'role': 'virtual_color', 'srgb': True,
        'compression': 'unreal.TextureCompressionSettings.TC_Default',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_VIRTUAL_COLOR',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_BASE_COLOR')],
    },
}

# Backward-compatible role lookup (param_name → role string)
TEX_PARAM_ROLES = {k: v['role'] for k, v in PARAM_CONNECTIONS.items()}


# ======================== Database Helper ========================

class DBHelper:
    """Read-only database helper that reuses the schema from ref_viewer.py."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Migration: add 'ue_imported' column if missing (NULL=not scanned, 1=imported, 0=not imported)
        try:
            self.conn.execute('ALTER TABLE assets ADD COLUMN ue_imported INTEGER DEFAULT NULL')
        except sqlite3.OperationalError:
            pass  # Column already exists
        # Migration: add 'tri_count' and 'glb_size' columns for mesh stats
        try:
            self.conn.execute('ALTER TABLE assets ADD COLUMN tri_count INTEGER DEFAULT NULL')
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute('ALTER TABLE assets ADD COLUMN glb_size INTEGER DEFAULT NULL')
        except sqlite3.OperationalError:
            pass

    def get_meta(self, key):
        r = self.conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return r[0] if r else None

    def search_static_meshes(self, query=''):
        q = f"%{query}%" if query else "%"
        return [dict(r) for r in self.conn.execute(
            'SELECT * FROM assets WHERE type=? AND (name LIKE ? OR package_path LIKE ?) ORDER BY name',
            ('StaticMesh', q, q))]

    def search_all_assets(self, query=''):
        q = f"%{query}%" if query else "%"
        return [dict(r) for r in self.conn.execute(
            'SELECT * FROM assets WHERE (name LIKE ? OR package_path LIKE ?) ORDER BY type, name', (q, q))]

    def get_by_path(self, pp):
        r = self.conn.execute('SELECT * FROM assets WHERE package_path=?', (pp,)).fetchone()
        return dict(r) if r else None

    def update_ue_imported(self, package_path, val):
        """Update the ue_imported column for a single asset.
        val: 1=imported, 0=not imported, None=not scanned"""
        self.conn.execute('UPDATE assets SET ue_imported=? WHERE package_path=?', (val, package_path))
        self.conn.commit()

    def batch_update_ue_imported(self, updates):
        """Batch update ue_imported. updates: dict {package_path: val}"""
        self.conn.executemany('UPDATE assets SET ue_imported=? WHERE package_path=?',
                              [(v, k) for k, v in updates.items()])
        self.conn.commit()

    def batch_update_mesh_stats(self, updates):
        """Batch update tri_count and glb_size. updates: dict {package_path: (tri_count, glb_size)}"""
        self.conn.executemany('UPDATE assets SET tri_count=?, glb_size=? WHERE package_path=?',
                              [(v[0], v[1], k) for k, v in updates.items()])
        self.conn.commit()

    def get_mesh_stats_map(self):
        """Return {package_path: (tri_count, glb_size)} for all assets."""
        return {r['package_path']: (r['tri_count'], r['glb_size']) for r in
                self.conn.execute('SELECT package_path, tri_count, glb_size FROM assets')}

    def get_ue_imported_map(self):
        """Return {package_path: ue_imported} for all assets."""
        return {r['package_path']: r['ue_imported'] for r in
                self.conn.execute('SELECT package_path, ue_imported FROM assets')}

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
    With the unified export root, both JSON metadata and binary assets
    (.glb, .png) live under the same directory tree.
    The FModel export root may be I:\\FModelOutput\\Exports, but actual
    assets live under I:\\FModelOutput\\Exports\\SLASHER\\Content\\.
    The /Game/ prefix maps to the Content directory, so we need to try
    both the root and root/SLASHER/Content as candidates.
    """
    roots = [fmodel_root]
    # FModel exports often have a Content subdirectory (e.g. SLASHER/Content)
    # The /Game/ path maps to Content, so files are under root/SLASHER/Content/
    for sub in ('SLASHER/Content', 'Content'):
        candidate = os.path.join(fmodel_root, sub)
        if os.path.isdir(candidate) and candidate not in roots:
            roots.append(candidate)
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


def find_material_json(ue_path, fmodel_root, prefer_raw=False):
    """Find the material JSON file for a given UE material path.
    By default prefers the simplified format (dict with Textures/Parameters keys).
    If prefer_raw=True, prefers the FModel raw export format (list with Properties)."""
    rel = ue_path.replace('/Game/', '', 1) if ue_path.startswith('/Game/') else ue_path
    candidates = []
    for root in get_fmodel_roots(fmodel_root):
        json_path = os.path.join(root, rel + '.json')
        if os.path.isfile(json_path):
            candidates.append(json_path)
    if not candidates:
        return None
    
    if prefer_raw:
        # Prefer FModel raw format (list with Properties/CachedExpressionData)
        for jp in candidates:
            try:
                with open(jp, 'r', encoding='utf-8-sig') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    for e in data:
                        if isinstance(e, dict) and ('Properties' in e or 'CachedExpressionData' in e):
                            return jp
            except Exception:
                pass
    else:
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
    for suffix, role in [('_VIRTUALMASK', 'virtual_mask'),
                          ('_VIRTUALCOLOR', 'virtual_color'),
                          ('_ORM', 'orm'), ('_NORMAL', 'normal'), ('_N', 'normal'),
                          ('_DISPLACEMENT', 'mask'), ('_HEIGHT', 'mask'),
                          ('_M', 'mask'), ('_C', 'mask'),
                          ('_ALBEDO', 'diffuse'), ('_BASECOLOR', 'diffuse'),
                          ('_A', 'diffuse')]:
        if n.endswith(suffix):
            return role
    # Check for common keywords
    if 'VIRTUALMASK' in n:
        return 'virtual_mask'
    if 'VIRTUALCOLOR' in n:
        return 'virtual_color'
    if 'NORMAL' in n or 'NORM' in n:
        return 'normal'
    if 'ORM' in n or 'ROUGH' in n:
        return 'orm'
    if 'MASK' in n:
        return 'mask'
    return 'diffuse'


def resolve_master_material(mat_ue_path, fmodel_root):
    """Follow the Parent chain in material JSON to find the root Master Material.
    Returns (master_json_path, master_json_data) or (None, None) if not found."""
    visited = set()
    current_path = mat_ue_path
    while current_path and current_path not in visited:
        visited.add(current_path)
        json_file = find_material_json(current_path, fmodel_root, prefer_raw=True)
        if not json_file:
            break
        try:
            with open(json_file, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
        except Exception:
            break
        
        # Handle both simplified and FModel raw formats
        parent_path = None
        
        if isinstance(data, list):
            # FModel raw format: look for Properties.Parent
            for e in data:
                if isinstance(e, dict) and 'Properties' in e:
                    parent = e['Properties'].get('Parent', {})
                    obj_path = parent.get('ObjectPath', '') if isinstance(parent, dict) else ''
                    if obj_path:
                        parent_path = norm_path(obj_path)
                        break
        elif isinstance(data, dict):
            # Simplified format: may have 'Parent' key directly
            parent_ref = data.get('Parent', '')
            if isinstance(parent_ref, str) and parent_ref:
                parent_path = norm_path(parent_ref)
            elif isinstance(parent_ref, dict):
                obj_path = parent_ref.get('ObjectPath', '')
                if obj_path:
                    parent_path = norm_path(obj_path)
        
        if not parent_path:
            # No parent - this IS the master material
            return json_file, data
        current_path = parent_path
    return None, None


def get_param_connection(param_name, master_conn_map=None):
    """Look up how a texture parameter connects to material outputs.
    If master_conn_map is provided (built from Master Material JSON), use it.
    Otherwise fall back to PARAM_CONNECTIONS or param name heuristics."""
    if master_conn_map and param_name in master_conn_map:
        return master_conn_map[param_name]
    conn = PARAM_CONNECTIONS.get(param_name)
    if conn:
        return conn
    # Fallback: try to infer from param name
    pn = param_name.lower()
    if 'virtualmask' in pn:
        return PARAM_CONNECTIONS['VirtualMask']
    if 'virtualcolor' in pn:
        return PARAM_CONNECTIONS['VirtualColor']
    if 'albedo' in pn or 'diffuse' in pn or 'base' in pn or 'color' in pn:
        return PARAM_CONNECTIONS['Base_Albedo']
    if 'normal' in pn or 'norm' in pn:
        return PARAM_CONNECTIONS['Normals']
    if 'orm' in pn or 'roughness' in pn or 'specular' in pn:
        return PARAM_CONNECTIONS['ORM']
    if 'mask' in pn or 'cavity' in pn or 'convex' in pn or 'thickness' in pn:
        return PARAM_CONNECTIONS['Masks']
    # Unknown - treat as color/diffuse by default
    return {
        'role': 'diffuse', 'srgb': True,
        'compression': 'unreal.TextureCompressionSettings.TC_Default',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_BASE_COLOR')],
    }


def build_param_connections_from_master(master_json_data):
    """Build a deterministic param_name → connection spec mapping from Master Material JSON.
    
    Parses CachedExpressionData.RuntimeEntries[3].ParameterInfoSet (texture param names)
    and CachedExpressionData.TextureValues (default textures, by index correspondence)
    to determine each parameter's role from the default texture's filename suffix.
    
    This is NOT guessing — it uses the actual default textures assigned in the Master Material
    to deterministically determine what data each parameter carries.
    """
    entries = master_json_data if isinstance(master_json_data, list) else [master_json_data]
    param_names = []
    default_textures = []
    
    for e in entries:
        if not isinstance(e, dict):
            continue
        cached = e.get('CachedExpressionData', {})
        # RuntimeEntries[3] = texture parameters
        re3 = cached.get('RuntimeEntries[3]', {})
        for p in re3.get('ParameterInfoSet', []):
            param_names.append(p.get('Name', ''))
        # TextureValues = default textures (same index order as RuntimeEntries[3])
        for tv in cached.get('TextureValues', []):
            default_textures.append(os.path.basename(tv.get('AssetPathName', '')))
    
    conn_map = {}
    for i, pname in enumerate(param_names):
        default_tex = default_textures[i] if i < len(default_textures) else ''
        # Derive role from default texture suffix — this is deterministic
        role = guess_texture_role(default_tex) if default_tex else None
        # If suffix-based detection is inconclusive, use param name semantics
        if not role or role == 'diffuse':
            pn = pname.lower()
            if 'virtualmask' in pn:
                role = 'virtual_mask'
            elif 'virtualcolor' in pn:
                role = 'virtual_color'
            elif 'normal' in pn:
                role = 'normal'
            elif 'orm' in pn or 'roughness' in pn or 'specular' in pn:
                role = 'orm'
            elif 'mask' in pn or 'cavity' in pn or 'convex' in pn or 'thickness' in pn or 'height' in pn or 'displacement' in pn:
                role = 'mask'
            elif 'albedo' in pn or 'base' in pn or 'color' in pn or 'sand' in pn:
                role = 'diffuse'
            elif not role:
                role = 'diffuse'
        conn = _role_to_connection(role)
        conn_map[pname] = conn
    
    return conn_map


def _role_to_connection(role):
    """Map a texture role to a full connection spec."""
    for conn in PARAM_CONNECTIONS.values():
        if conn['role'] == role:
            return conn
    # Fallback
    return {
        'role': 'diffuse', 'srgb': True,
        'compression': 'unreal.TextureCompressionSettings.TC_Default',
        'sampler_type': 'unreal.MaterialSamplerType.SAMPLERTYPE_COLOR',
        'connections': [('RGB', 'unreal.MaterialProperty.MP_BASE_COLOR')],
    }


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
        result['json_file'] = json_path if (json_path and os.path.isfile(json_path)) else None
        result['glb_file'] = glb_path if (glb_path and os.path.isfile(glb_path)) else None
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
                        mi = sm.get('MaterialInterface')
                        if not mi or not isinstance(mi, dict):
                            continue
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
                    # 3. Master Material fallback: ReferencedTextures (no param names)
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
                                            # ObjectPath is like "/Game/.../T_Atlass_A_C.T_Atlass_A_C"
                                            # ObjectName is like "Texture2D'T_Atlass_A_C'" (no path)
                                            # Prefer ObjectPath since it always has the full /Game/ path
                                            tex_path = val.get('ObjectPath', '') or val.get('ObjectName', '')
                                            if tex_path and tex_path.startswith('/Game/'):
                                                tex_path = norm_path(tex_path)
                                            elif tex_path and "'" in tex_path:
                                                # Extract from "Texture2D'T_Atlass_A_C'" format
                                                # Fallback: no /Game/ path available, skip
                                                pass
                                            if param_name and tex_path and tex_path.startswith('/Game/'):
                                                textures[param_name] = tex_path
                                # If no TextureParameterValues textures found, try ReferencedTextures
                                # (Master Materials store texture refs at top level and/or in
                                # CachedExpressionData, NOT in Properties.ReferencedTextures)
                                if not textures:
                                    ref_texs = []
                                    # Top-level ReferencedTextures
                                    ref_texs.extend(e.get('ReferencedTextures', []))
                                    # CachedExpressionData.ReferencedTextures
                                    ced = e.get('CachedExpressionData', {})
                                    if isinstance(ced, dict):
                                        ref_texs.extend(ced.get('ReferencedTextures', []))
                                    for rt in ref_texs:
                                        if isinstance(rt, dict):
                                            tex_path = rt.get('ObjectPath', '')
                                            if tex_path and tex_path.startswith('/Game/'):
                                                tex_norm = norm_path(tex_path)
                                                tex_name = os.path.basename(tex_norm)
                                                role = guess_texture_role(tex_name)
                                                if role:
                                                    textures[role] = tex_norm
                                break
                    
                    # Resolve Master Material to build deterministic param connection map
                    master_path, master_data = resolve_master_material(mat_path, self.fmodel_root)
                    param_conn_map = None
                    if master_data:
                        param_conn_map = build_param_connections_from_master(master_data)
                    
                    # Build a set of valid param names from master material
                    valid_param_names = set(param_conn_map.keys()) if param_conn_map else None
                    
                    # Track which textures we've already assigned to avoid duplicates
                    seen_tex_in_section = set()
                    
                    for param_name, tex_path in textures.items():
                        if not isinstance(tex_path, str) or not tex_path:
                            continue
                        tex_norm = norm_path(tex_path)
                        if tex_norm.startswith('/Script/') or tex_norm.startswith('/Engine/'):
                            continue
                        # Filter: if we have a master material param map, only accept
                        # param names that exist in it. This excludes junk keys like
                        # texture filenames (T_Tiles_A_Bake_02_M) and legacy aliases
                        # (PM_Diffuse) that don't match the master material's actual params.
                        # Exception: role-based keys (diffuse, normal, orm, mask) from
                        # ReferencedTextures are always accepted since they represent
                        # standard PBR texture slots.
                        is_role_key = param_name in ('diffuse', 'normal', 'orm', 'mask')
                        if valid_param_names is not None and param_name not in valid_param_names and not is_role_key:
                            continue
                        # Deduplicate: skip if this texture was already assigned via another param
                        if tex_norm in seen_tex_in_section:
                            continue
                        seen_tex_in_section.add(tex_norm)
                        tex_local = find_texture_file(tex_norm, self.fmodel_root)
                        conn = get_param_connection(param_name, param_conn_map)
                        role = conn['role']
                        # Store by param_name for accurate material building
                        if param_name not in section['textures']:
                            section['textures'][param_name] = {
                                'ue_path': tex_norm,
                                'local_file': tex_local,
                                'name': os.path.basename(tex_norm),
                                'param_name': param_name,
                                'role': role,
                            }
                        if tex_norm not in seen_textures:
                            result['all_textures'].append({
                                'ue_path': tex_norm,
                                'local_file': tex_local,
                                'name': os.path.basename(tex_norm),
                                'role': role,
                                'param_name': param_name,
                            })
                            seen_textures[tex_norm] = True
                except Exception:
                    pass
            else:
                # Engine built-in materials (e.g. WorldGridMaterial) don't have JSON files
                # but exist in UE natively — mark as found, don't add to missing
                if mat_path.startswith('/Engine/') or mat_path.startswith('/Script/'):
                    section['found'] = True
                    section['material_type'] = sm['material_type'] + ' (Engine)'
                    if mat_path not in seen_materials:
                        mat_info = {
                            'ue_path': mat_path,
                            'type': sm['material_type'] + ' (Engine)',
                            'json_file': None,
                            'name': os.path.basename(mat_path),
                        }
                        result['all_materials'].append(mat_info)
                        seen_materials[mat_path] = mat_info
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
    def import_texture(local_file, dest_ue_path, param_name='', param_conn_map=None):
        """Build command to import a texture into UE.
        Uses param_name + master material data to determine correct compression/sRGB."""
        name = os.path.basename(dest_ue_path)
        dest_dir = os.path.dirname(dest_ue_path)
        # Use forward slashes for UE paths
        dest_dir = dest_dir.replace('\\', '/')

        local_file_fwd = local_file.replace('\\', '/')

        conn = get_param_connection(param_name, param_conn_map)
        compression_settings = conn['compression']
        srgb = 'True' if conn['srgb'] else 'False'
        role = conn['role']

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
    def fix_texture_settings(dest_ue_path, param_name='', param_conn_map=None):
        """Build command to fix texture settings for an already-imported texture.
        Uses param_name + master material data to determine correct settings."""
        conn = get_param_connection(param_name, param_conn_map)
        compression_settings = conn['compression']
        srgb = 'True' if conn['srgb'] else 'False'
        role = conn['role']

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
    def create_material(dest_ue_path, tex_assignments, material_name=None, param_conn_map=None):
        """Build command to create a simple material with textures connected.
        tex_assignments: list of {ue_path, param_name, role}
        Connections are driven by param_name via get_param_connection() with master material data.
        """
        name = material_name or os.path.basename(dest_ue_path)
        dest_dir = os.path.dirname(dest_ue_path).replace('\\', '/')

        # Build texture sampler setup commands using param_name-driven connections
        tex_cmds = []
        y_offset = 0
        for i, ta in enumerate(tex_assignments):
            param_name = ta.get('param_name', '')
            tex_path = ta['ue_path']
            tex_obj = tex_path + '.' + os.path.basename(tex_path)

            conn = get_param_connection(param_name, param_conn_map)
            sampler_type = conn['sampler_type']
            connections = conn['connections']
            # Use a unique variable name per texture to avoid collisions
            var_name = f"ts_{i}"

            conn_lines = []
            for channel, mp_prop in connections:
                conn_lines.append(
                    f'        unreal.MaterialEditingLibrary.connect_material_property({var_name}, "{channel}", {mp_prop})'
                )
            conn_block = '\n'.join(conn_lines) if conn_lines else '        pass  # no direct output connection'

            tex_cmds.append(f'''
try:
    tex_obj_{i} = unreal.load_asset("{tex_obj}")
    if tex_obj_{i}:
        {var_name} = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionTextureSample, -400, {y_offset})
        {var_name}.set_editor_property("texture", tex_obj_{i})
        {var_name}.set_editor_property("sampler_type", {sampler_type})
{conn_block}
except Exception as e:
    print(f"Texture {{param_name}} error: {{e}}")
''')
            y_offset += 200

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
        self.fmodel_root = self.db.get_meta('last_dir') if self.db else r"I:\FModelOutput\Exports"
        self.content_root = "/Game/Developers/bosonhuang/SlasherAsset"
        self.ue_host = "239.0.0.1"
        self.ue_port = 6766
        self.current_resolution = None
        self.tree_map = {}
        # Wireframe preview state
        self._preview_mode = None
        self._mesh_verts = None
        self._mesh_edges = None
        self._mesh_meta = None
        self._mesh_rot_x = 0.0
        self._mesh_rot_y = 0.0
        self._mesh_zoom = 1.0
        self._preview_token = None
        self._drag_last = None

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

        # Main area: PanedWindow for resizable panels
        paned = ttk.PanedWindow(self.root, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=8, pady=4)

        # Left: asset search + tree
        left = ttk.Frame(paned)

        ttk.Label(left, text="Search StaticMesh:").pack(anchor='w')
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', lambda *_: self._filter_tree())
        ttk.Entry(left, textvariable=self.search_var).pack(fill='x', pady=(0, 4))
        self.glb_only_var = tk.BooleanVar(value=False)
        self.glb_only_var.trace_add('write', lambda *_: self._filter_tree())
        ttk.Checkbutton(left, text="Only show meshes with GLB",
                        variable=self.glb_only_var).pack(anchor='w', pady=(0, 4))
        self.not_imported_var = tk.BooleanVar(value=False)
        self.not_imported_var.trace_add('write', lambda *_: self._filter_tree())
        ttk.Checkbutton(left, text="Only show not imported to UE",
                        variable=self.not_imported_var).pack(anchor='w', pady=(0, 4))
        self.refresh_ue_btn = ttk.Button(left, text="Refresh UE Import Status",
                                         command=self._refresh_ue_import_status)
        self.refresh_ue_btn.pack(fill='x', pady=(0, 4))
        # Mesh stats (tri_count, glb_size) are now populated during database build in ref_viewer.py
        # No separate scan button needed - data is read directly from the database.
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=('type', 'glb', 'ue', 'tris', 'size'), show='tree headings', selectmode='extended')
        self.tree.heading('#0', text='Asset')
        self.tree.heading('type', text='Type')
        self.tree.heading('glb', text='GLB')
        self.tree.heading('ue', text='UE')
        self.tree.heading('tris', text='Tris')
        self.tree.heading('size', text='Size')
        self.tree.column('#0', width=200)
        self.tree.column('type', width=70)
        self.tree.column('glb', width=50, anchor='center')
        self.tree.column('ue', width=50, anchor='center')
        self.tree.column('tris', width=60, anchor='e')
        self.tree.column('size', width=70, anchor='e')
        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        self.tree.bind('<Double-1>', self._on_tree_dblclick)
        self.tree.bind('<Button-3>', self._tree_right_click)
        paned.add(left, weight=1)

        # Center: Notebook with Details and Preview tabs
        center = ttk.Frame(paned)
        paned.add(center, weight=2)

        self.center_nb = ttk.Notebook(center)
        self.center_nb.pack(fill='both', expand=True)

        # --- Tab 1: Reference Details ---
        details_tab = ttk.Frame(self.center_nb)
        self.center_nb.add(details_tab, text="Details")

        detail_frame = ttk.Frame(details_tab)
        detail_frame.pack(fill='both', expand=True)

        self.detail_text = tk.Text(detail_frame, wrap='word', state='disabled',
                                   bg='#1E1E1E', fg='#DDDDDD', font=('Consolas', 9),
                                   insertbackground='white')
        dsb = ttk.Scrollbar(detail_frame, orient='vertical', command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=dsb.set)
        self.detail_text.pack(side='left', fill='both', expand=True)
        dsb.pack(side='right', fill='y')

        # Buttons
        btn_frame = ttk.Frame(details_tab)
        btn_frame.pack(fill='x', pady=4)
        self.resolve_btn = ttk.Button(btn_frame, text="Resolve References", command=self._resolve_selected)
        self.resolve_btn.pack(side='left', padx=4)
        self.import_btn = ttk.Button(btn_frame, text="Import to UE", command=self._start_import, state='disabled')
        self.import_btn.pack(side='left', padx=4)
        self.batch_import_btn = ttk.Button(btn_frame, text="Batch Import to UE", command=self._start_batch_import, state='normal')
        self.batch_import_btn.pack(side='left', padx=4)
        self.skip_glb_var = tk.BooleanVar(value=True)
        self.skip_glb_cb = ttk.Checkbutton(btn_frame, text="Skip existing GLB", variable=self.skip_glb_var)
        self.skip_glb_cb.pack(side='left', padx=4)

        # --- Tab 2: Wireframe Preview ---
        preview_tab = ttk.Frame(self.center_nb)
        self.center_nb.add(preview_tab, text="Preview")

        self.preview_canvas = tk.Canvas(preview_tab, bg='#2B2B2B', highlightthickness=0)
        self.preview_canvas.pack(fill='both', expand=True)
        self.preview_canvas.bind('<ButtonPress-1>', self._preview_drag_start)
        self.preview_canvas.bind('<B1-Motion>', self._preview_drag_move)
        self.preview_canvas.bind('<ButtonRelease-1>', self._preview_drag_end)
        self.preview_canvas.bind('<MouseWheel>', self._preview_wheel)
        self.preview_canvas.bind('<Button-4>', self._preview_wheel)
        self.preview_canvas.bind('<Button-5>', self._preview_wheel)
        self.preview_canvas.bind('<Double-Button-1>', self._preview_reset_view)

        # Right: log
        right = ttk.Frame(paned)

        ttk.Label(right, text="Import Log", font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        log_frame = ttk.Frame(right)
        log_frame.pack(fill='both', expand=True)
        self.log_text = tk.Text(log_frame, wrap='word', state='disabled',
                                bg='#1E1E1E', fg='#88FF88', font=('Consolas', 9))
        self.log_text.tag_configure('warning', foreground='#FFA500')
        self.log_text.tag_configure('error', foreground='#FF4444')
        lsb = ttk.Scrollbar(log_frame, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        lsb.pack(side='right', fill='y')
        self.log_text.bind('<Button-3>', self._log_right_click)
        paned.add(right, weight=1)

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

    def _refresh_ue_import_status(self):
        """Check which meshes have already been imported to UE and update the database."""
        if self.importing:
            messagebox.showwarning("Busy", "Import in progress, please wait.")
            return
        host = self.host_var.get()
        port = int(self.port_var.get())
        content_root = self.content_var.get()
        if not content_root:
            messagebox.showwarning("Missing Config", "Please set UE Content Root first.")
            return
        self.refresh_ue_btn.config(state='disabled')
        self._log("\n--- Refreshing UE Import Status ---")
        self._log(f"  Content Root: {content_root}")
        t = threading.Thread(target=self._refresh_ue_worker, args=(host, port, content_root), daemon=True)
        t.start()

    def _refresh_ue_worker(self, host, port, content_root):
        """Worker thread: query UE for all StaticMesh assets under content_root."""
        from ue_remote import UERemoteExec
        try:
            client = UERemoteExec(host, port)
            if not client.connect(timeout=15):
                self.queue.put(('log', "  ✗ Cannot connect to UE"))
                self.queue.put(('ue_refresh_done', False, "Connection failed"))
                return
            self.queue.put(('log', "  Connected, scanning UE assets..."))

            # Query all assets under content_root, filter StaticMesh
            cmd = f'''
import unreal
results = []
all_paths = unreal.EditorAssetLibrary.list_assets("{content_root}", recursive=True)
for p in all_paths:
    ad = unreal.EditorAssetLibrary.find_asset_data(p)
    if ad and ad.asset_class_path.asset_name == "StaticMesh":
        results.append(p)
print("RESULT::" + "|".join(results))
'''
            r = client.run_command(cmd, mode='exec', timeout=120)
            client.disconnect()

            out_text = ''
            out = r.get('output', '')
            if isinstance(out, list):
                out_text = ''.join(str(item.get('output', '')) if isinstance(item, dict) else str(item) for item in out)
            else:
                out_text = str(out)

            ue_meshes = set()
            for line in out_text.splitlines():
                line = line.strip()
                if line.startswith('RESULT::'):
                    paths_str = line[len('RESULT::'):]
                    if paths_str:
                        for p in paths_str.split('|'):
                            p = p.strip()
                            if p:
                                # UE returns object paths like /Game/.../SM_xxx.SM_xxx
                                # Convert to package path by stripping the .ObjectName suffix
                                pkg = p.rsplit('.', 1)[0] if '.' in p else p
                                ue_meshes.add(pkg)
                    break

            # Build mapping: for each mesh in DB, check if its import dest exists in UE
            assets = self.db.search_static_meshes('')
            updates = {}
            imported_count = 0
            for a in assets:
                pp = a['package_path']
                dest = ue_path_to_import_dest(pp, content_root)
                # Also try matching with .ObjectName suffix stripped from dest for safety
                dest_pkg = dest.rsplit('.', 1)[0] if '.' in dest else dest
                if dest in ue_meshes or dest_pkg in ue_meshes:
                    updates[pp] = 1
                    imported_count += 1
                else:
                    updates[pp] = 0

            self.db.batch_update_ue_imported(updates)
            self.queue.put(('log', f"  ✓ Scanned {len(assets)} meshes, {imported_count} found in UE"))
            self.queue.put(('ue_refresh_done', True, f"{imported_count}/{len(assets)} imported"))
        except Exception as e:
            self.queue.put(('log', f"  ✗ ERROR: {e}"))
            self.queue.put(('ue_refresh_done', False, str(e)))

    def _refresh_tree(self):
        self._filter_tree()

    def _filter_tree(self):
        q = self.search_var.get()
        glb_only = getattr(self, 'glb_only_var', None) and self.glb_only_var.get()
        not_imported = getattr(self, 'not_imported_var', None) and self.not_imported_var.get()
        self.tree.delete(*self.tree.get_children())
        self.tree_map.clear()
        if not self.db:
            return
        assets = self.db.search_static_meshes(q)
        # Build a cache of ue_imported status
        ue_map = self.db.get_ue_imported_map()
        # Build a cache of mesh stats (tri_count, glb_size)
        stats_map = self.db.get_mesh_stats_map()
        for a in assets:
            exported = a.get('exported', 0)
            if glb_only and not exported:
                continue
            ue_val = ue_map.get(a['package_path'])
            if not_imported and ue_val == 1:
                continue
            glb_mark = '✓' if exported else '✗'
            ue_val = ue_map.get(a['package_path'])
            if ue_val is None:
                ue_mark = '?'
            elif ue_val == 1:
                ue_mark = '✓'
            else:
                ue_mark = '✗'
            tris_val, size_val = stats_map.get(a['package_path'], (None, None))
            tris_str = self._fmt_tris(tris_val)
            size_str = self._fmt_size(size_val)
            item = self.tree.insert('', 'end', text=a['name'],
                                    values=(a['type'], glb_mark, ue_mark, tris_str, size_str))
            self.tree_map[item] = a['package_path']

    @staticmethod
    def _fmt_tris(n):
        """Format triangle count: 1300000 -> '1.3M', 248000 -> '248K', 500 -> '500'."""
        if n is None:
            return '-'
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        elif n >= 1_000:
            return f"{n // 1_000}K"
        return str(n)

    @staticmethod
    def _fmt_size(n):
        """Format file size in bytes to human-readable: 1048576 -> '1.0MB'."""
        if n is None:
            return '-'
        if n >= 1_073_741_824:
            return f"{n / 1_073_741_824:.1f}GB"
        elif n >= 1_048_576:
            return f"{n / 1_048_576:.1f}MB"
        elif n >= 1024:
            return f"{n / 1024:.0f}KB"
        return f"{n}B"

    def _on_tree_select(self, e):
        sel = self.tree.selection()
        if not sel:
            return
        pp = self.tree_map.get(sel[0])
        if pp:
            self._show_asset_info(pp)
            self._update_wireframe(pp)

    def _on_tree_dblclick(self, e):
        self._resolve_selected()

    def _tree_right_click(self, e):
        """Right-click context menu on the asset tree."""
        item = self.tree.identify_row(e.y)
        if item:
            # If the item is not already selected, select only it
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            menu = tk.Menu(self.root, tearoff=0)
            sel_count = len(self.tree.selection())
            if sel_count > 1:
                menu.add_command(label=f"Batch Import to UE ({sel_count} meshes)", command=self._start_batch_import)
                menu.add_separator()
            menu.add_command(label="Resolve References", command=self._resolve_selected)
            menu.add_command(label="Import to UE (single)", command=self._start_import)
            menu.add_separator()
            menu.add_command(label="Copy UE Path", command=self._copy_selected_path)
            menu.tk_popup(e.x_root, e.y_root)

    def _copy_selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return
        paths = [self.tree_map.get(s, '') for s in sel]
        text = '\n'.join(p for p in paths if p)
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._log(f"Copied {len(paths)} path(s) to clipboard")

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

    # ======================== Wireframe Preview ========================

    def _update_wireframe(self, mesh_path):
        """Update wireframe preview when a StaticMesh is selected."""
        self.preview_canvas.delete('all')
        self._preview_mode = None
        self._preview_token = None
        cw = max(self.preview_canvas.winfo_width(), 280)
        ch = max(self.preview_canvas.winfo_height(), 200)

        json_path, glb_path = ue_path_to_local(mesh_path, self.fmodel_var.get())
        if glb_path and os.path.isfile(glb_path):
            self._render_wireframe_glb(glb_path)
        elif json_path and os.path.isfile(json_path):
            self._render_wireframe(json_path)
        else:
            self.preview_canvas.create_text(cw // 2, ch // 2, anchor='center',
                                            text='No mesh file found', fill='#888',
                                            font=('Segoe UI', 9))

    def _render_wireframe(self, json_path):
        """Parse vertex/index data from a StaticMesh JSON and draw wireframe."""
        try:
            with open(json_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            entries = data if isinstance(data, list) else [data]
            verts, indices = [], []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                props = e.get('Properties') or {}
                agg = props.get('AggGeom') or {}
                for ce in agg.get('ConvexElems', []):
                    vd = ce.get('VertexData', [])
                    base = len(verts)
                    for v in vd:
                        verts.append((v['X'], v['Y'], v['Z']))
                    idata = ce.get('IndexData', [])
                    for i in range(0, len(idata), 3):
                        if i + 2 < len(idata):
                            indices.append((idata[i] + base, idata[i + 1] + base, idata[i + 2] + base))
            if not verts:
                cw = max(self.preview_canvas.winfo_width(), 280)
                ch = max(self.preview_canvas.winfo_height(), 200)
                self.preview_canvas.create_text(cw // 2, ch // 2, anchor='center',
                                                text='No vertex data', fill='#888',
                                                font=('Segoe UI', 8))
                return
            self._draw_wireframe(verts, indices)
        except Exception as e:
            cw = max(self.preview_canvas.winfo_width(), 280)
            ch = max(self.preview_canvas.winfo_height(), 200)
            self.preview_canvas.create_text(cw // 2, ch // 2, anchor='center',
                                            text=f'Error: {e}', fill='#A55',
                                            font=('Segoe UI', 8))

    def _render_wireframe_glb(self, glb_path):
        """Parse a GLB in a background thread, then draw wireframe on the main thread."""
        cw = max(self.preview_canvas.winfo_width(), 280)
        ch = max(self.preview_canvas.winfo_height(), 200)
        self.preview_canvas.create_text(cw // 2, ch // 2, anchor='center',
                                        text='Loading mesh...', fill='#888',
                                        font=('Segoe UI', 9))
        token = object()
        self._preview_token = token

        def worker():
            try:
                result = self._parse_glb(glb_path)
                err = None
            except Exception as e:
                result, err = None, str(e)

            def apply():
                if getattr(self, '_preview_token', None) is not token:
                    return
                self.preview_canvas.delete('all')
                if err:
                    self.preview_canvas.create_text(cw // 2, ch // 2, anchor='center',
                                                    text=f'Error: {err}', fill='#A55',
                                                    font=('Segoe UI', 8))
                elif not result or not result[0]:
                    self.preview_canvas.create_text(cw // 2, ch // 2, anchor='center',
                                                    text='No vertex data in GLB', fill='#888',
                                                    font=('Segoe UI', 8))
                else:
                    verts, indices, total_tris = result
                    self._draw_wireframe(verts, indices, total_tris)
            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _parse_glb(glb_path):
        """Parse GLB geometry for wireframe preview. Returns (verts, indices, total_tris)."""
        import struct
        from pygltflib import GLTF2
        MAX_TRIS = 20000
        gltf = GLTF2().load(glb_path)
        blob = gltf.binary_blob()
        if not blob:
            return ([], [], 0)

        prims = []
        total_tris = 0
        for mesh in gltf.meshes:
            for prim in mesh.primitives:
                if prim.mode not in (None, 4):
                    continue
                if prim.attributes.POSITION is None:
                    continue
                pos_acc = gltf.accessors[prim.attributes.POSITION]
                bv = gltf.bufferViews[pos_acc.bufferView]
                pstart = (bv.byteOffset or 0) + (pos_acc.byteOffset or 0)
                pstride = bv.byteStride or 12
                pcount = pos_acc.count
                entry = {'pstart': pstart, 'pstride': pstride, 'pcount': pcount}
                if prim.indices is not None:
                    idx_acc = gltf.accessors[prim.indices]
                    ibv = gltf.bufferViews[idx_acc.bufferView]
                    istart = (ibv.byteOffset or 0) + (idx_acc.byteOffset or 0)
                    fmt, sz = {5121: ('<B', 1), 5123: ('<H', 2), 5125: ('<I', 4)}.get(
                        idx_acc.componentType, ('<I', 4))
                    entry.update({'indexed': True, 'istart': istart, 'fmt': fmt, 'sz': sz,
                                  'tri_count': idx_acc.count // 3})
                else:
                    entry.update({'indexed': False, 'tri_count': pcount // 3})
                prims.append(entry)
                total_tris += entry['tri_count']

        if total_tris == 0:
            return ([], [], 0)

        step = max(1, total_tris // MAX_TRIS)
        verts, indices = [], []
        vert_cache = {}

        def get_vert(pstart, pstride, local_vi):
            key = (pstart, local_vi)
            cached = vert_cache.get(key)
            if cached is not None:
                return cached
            off = pstart + local_vi * pstride
            x, y, z = struct.unpack_from('<fff', blob, off)
            idx = len(verts)
            verts.append((x, y, z))
            vert_cache[key] = idx
            return idx

        for prim in prims:
            pstart, pstride, pcount = prim['pstart'], prim['pstride'], prim['pcount']
            if prim['indexed']:
                istart, fmt, sz = prim['istart'], prim['fmt'], prim['sz']
                for t in range(0, prim['tri_count'], step):
                    k = t * 3
                    a = struct.unpack_from(fmt, blob, istart + k * sz)[0]
                    b = struct.unpack_from(fmt, blob, istart + (k + 1) * sz)[0]
                    c = struct.unpack_from(fmt, blob, istart + (k + 2) * sz)[0]
                    if a < pcount and b < pcount and c < pcount:
                        indices.append((get_vert(pstart, pstride, a),
                                        get_vert(pstart, pstride, b),
                                        get_vert(pstart, pstride, c)))
            else:
                for t in range(0, prim['tri_count'], step):
                    base = t * 3
                    if base + 2 < pcount:
                        indices.append((get_vert(pstart, pstride, base),
                                        get_vert(pstart, pstride, base + 1),
                                        get_vert(pstart, pstride, base + 2)))

        return (verts, indices, total_tris)

    def _draw_wireframe(self, verts, indices, total_tris=None):
        """Prepare wireframe geometry, cache it, and draw with the current view."""
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        cz = (min(zs) + max(zs)) / 2
        centered = [(v[0] - cx, v[1] - cy, v[2] - cz) for v in verts]

        drawn_tris = len(indices)
        orig_tris = total_tris if total_tris is not None else drawn_tris
        n = len(centered)
        seen = set()
        edges = []
        for a, b, c in indices:
            if a >= n or b >= n or c >= n:
                continue
            for u, v in ((a, b), (b, c), (c, a)):
                e = (u, v) if u < v else (v, u)
                if e not in seen:
                    seen.add(e)
                    edges.append(e)

        ext = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0
        self._preview_mode = 'mesh'
        self._mesh_verts = centered
        self._mesh_edges = edges
        self._mesh_meta = (ext, orig_tris, drawn_tris)
        self._mesh_rot_x = -25.0
        self._mesh_rot_y = 30.0
        self._mesh_zoom = 1.0
        self._redraw_mesh()

    def _redraw_mesh(self):
        """Project cached mesh verts with current rotation/zoom and draw edges."""
        import math
        if not self._mesh_verts or not self._mesh_edges:
            return
        ext, orig_tris, shown = self._mesh_meta
        self.preview_canvas.delete('all')
        self.preview_canvas.update_idletasks()
        cw = max(self.preview_canvas.winfo_width(), 100)
        ch = max(self.preview_canvas.winfo_height(), 100)
        ay = math.radians(self._mesh_rot_y)
        ax = math.radians(self._mesh_rot_x)
        cosa, sina = math.cos(ay), math.sin(ay)
        cosx, sinx = math.cos(ax), math.sin(ax)
        proj = []
        for vx, vy, vz in self._mesh_verts:
            x1 = vx * cosa - vy * sina
            y1 = vx * sina + vy * cosa
            z1 = vz
            x2 = x1
            y2 = y1 * cosx - z1 * sinx
            proj.append((x2, y2))
        base_scale = min(cw, ch) * 0.76 / ext
        scale = base_scale * self._mesh_zoom
        ox, oy = cw / 2, ch / 2
        pts = [(ox + x * scale, oy - y * scale) for x, y in proj]
        for u, v in self._mesh_edges:
            x1, y1 = pts[u]
            x2, y2 = pts[v]
            self.preview_canvas.create_line(x1, y1, x2, y2, fill='#4A90D9', width=1)
        note = f"{orig_tris:,} tris"
        if orig_tris > shown:
            note += f" (~{shown:,} shown)"
        self.preview_canvas.create_text(6, ch - 6, anchor='sw', text=note,
                                        fill='#777', font=('Segoe UI', 7))
        self.preview_canvas.create_text(6, 6, anchor='nw',
                                        text='drag: rotate  |  wheel: zoom  |  dbl-click: reset',
                                        fill='#555', font=('Segoe UI', 7))

    def _preview_drag_start(self, e):
        self._drag_last = (e.x, e.y)

    def _preview_drag_move(self, e):
        if self._drag_last is None or self._preview_mode != 'mesh':
            return
        dx = e.x - self._drag_last[0]
        dy = e.y - self._drag_last[1]
        self._drag_last = (e.x, e.y)
        self._mesh_rot_y += dx * 0.5
        self._mesh_rot_x += dy * 0.5
        self._mesh_rot_x = max(-89.0, min(89.0, self._mesh_rot_x))
        self._redraw_mesh()

    def _preview_drag_end(self, e):
        self._drag_last = None

    def _preview_wheel(self, e):
        if getattr(e, 'num', None) == 5 or getattr(e, 'delta', 0) < 0:
            factor = 1 / 1.15
        else:
            factor = 1.15
        if self._preview_mode == 'mesh':
            self._mesh_zoom = max(0.1, min(30.0, self._mesh_zoom * factor))
            self._redraw_mesh()

    def _preview_reset_view(self, e):
        if self._preview_mode == 'mesh':
            self._mesh_rot_x = -25.0
            self._mesh_rot_y = 30.0
            self._mesh_zoom = 1.0
            self._redraw_mesh()

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
        self.fmodel_root = self.fmodel_var.get()
        self.importing = True
        self.import_btn.config(state='disabled')
        self.resolve_btn.config(state='disabled')
        self.batch_import_btn.config(state='disabled')
        self.pb['value'] = 0
        mesh_path = self.current_resolution['mesh_path']
        self._log(f"\n{'='*60}")
        self._log(f"Single Import (V2 pipeline): {mesh_path}")
        self._log(f"{'='*60}")
        t = threading.Thread(target=self._single_import_worker_v2, args=(mesh_path,), daemon=True)
        t.start()

    def _single_import_worker_v2(self, mesh_path):
        """Worker thread: run V2 MeshImportPipeline for a single mesh."""
        from mesh_importer_v2 import MeshImportPipeline

        mesh_name = os.path.basename(mesh_path)
        self.queue.put(('progress', 0, 1, f"[1/1] {mesh_name}"))
        self.queue.put(('log', f"\n--- [1/1] {mesh_name} ---"))

        json_path, glb_path = ue_path_to_local(mesh_path, self.fmodel_root)
        if not json_path:
            self.queue.put(('log', f"  ✗ JSON not found for {mesh_path}"))
            self.queue.put(('done',))
            return

        self.queue.put(('log', f"  JSON: {json_path}"))
        self.queue.put(('log', f"  GLB:  {glb_path or 'NOT FOUND'}"))

        try:
            pipeline = MeshImportPipeline(
                fmodel_root=self.fmodel_root,
                content_root=self.content_root,
                ue_host=self.ue_host,
                ue_port=self.ue_port,
                skip_existing_glb=self.skip_glb_var.get(),
            )
            pipeline.log = lambda msg: self.queue.put(('log', msg))
            ok = pipeline.run(json_path)
            if ok:
                self.queue.put(('log', f"  ✓ {mesh_name} imported successfully"))
            else:
                self.queue.put(('log', f"  ✗ {mesh_name} import failed"))
        except Exception as e:
            self.queue.put(('log', f"  ✗ ERROR: {e}"))

        self.queue.put(('done',))

    def _start_batch_import(self):
        """Start batch import using v2 pipeline for all selected meshes."""
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Please select one or more StaticMeshes in the list.")
            return
        if self.importing:
            return

        paths = [self.tree_map.get(s) for s in sel]
        paths = [p for p in paths if p]
        if not paths:
            messagebox.showwarning("No Assets", "No valid assets found in selection.")
            return

        # Confirm with user
        msg = f"Batch import {len(paths)} meshes using the V2 pipeline?\n\nAssets:\n" + "\n".join(f"  • {os.path.basename(p)}" for p in paths[:10])
        if len(paths) > 10:
            msg += f"\n  ... and {len(paths) - 10} more"
        if not messagebox.askyesno("Batch Import", msg):
            return

        self.content_root = self.content_var.get()
        self.ue_host = self.host_var.get()
        self.ue_port = int(self.port_var.get())
        self.fmodel_root = self.fmodel_var.get()
        self.importing = True
        self.batch_import_btn.config(state='disabled')
        self.import_btn.config(state='disabled')
        self.resolve_btn.config(state='disabled')
        self.pb['value'] = 0
        self._log(f"\n{'='*60}")
        self._log(f"Batch Import: {len(paths)} meshes (V2 pipeline)")
        self._log(f"{'='*60}")
        t = threading.Thread(target=self._batch_import_worker, args=(paths,), daemon=True)
        t.start()

    def _batch_import_worker(self, mesh_paths):
        """Worker thread: run V2 MeshImportPipeline for each selected mesh."""
        from mesh_importer_v2 import MeshImportPipeline

        total = len(mesh_paths)
        success_count = 0
        fail_count = 0
        failed_meshes = []  # (mesh_path, error_reason)

        for idx, mp in enumerate(mesh_paths):
            mesh_name = os.path.basename(mp)
            self.queue.put(('progress', idx, total, f"[{idx+1}/{total}] {mesh_name}"))
            self.queue.put(('log', f"\n--- [{idx+1}/{total}] {mesh_name} ---"))

            # Find the JSON file for this mesh
            json_path, glb_path = ue_path_to_local(mp, self.fmodel_root)
            if not json_path:
                self.queue.put(('log', f"  ✗ JSON not found for {mp}"))
                fail_count += 1
                failed_meshes.append((mp, "JSON not found"))
                continue
            if not glb_path:
                self.queue.put(('log', f"  ⚠ GLB not found, will attempt import anyway"))

            self.queue.put(('log', f"  JSON: {json_path}"))
            self.queue.put(('log', f"  GLB:  {glb_path or 'NOT FOUND'}"))

            try:
                pipeline = MeshImportPipeline(
                    fmodel_root=self.fmodel_root,
                    content_root=self.content_root,
                    ue_host=self.ue_host,
                    ue_port=self.ue_port,
                    skip_existing_glb=self.skip_glb_var.get(),
                )
                # Override pipeline's log method to feed into UI via queue (thread-safe)
                pipeline.log = lambda msg: self.queue.put(('log', msg))
                ok = pipeline.run(json_path)
                if ok:
                    success_count += 1
                    self.queue.put(('log', f"  ✓ {mesh_name} imported successfully"))
                else:
                    fail_count += 1
                    failed_meshes.append((mp, "pipeline.run() returned False"))
                    self.queue.put(('log', f"  ✗ {mesh_name} import failed"))
            except Exception as e:
                fail_count += 1
                failed_meshes.append((mp, str(e)))
                self.queue.put(('log', f"  ✗ ERROR: {e}"))

        # Write error log if there were failures
        if failed_meshes:
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            log_dir = os.path.join(SCRIPT_DIR, "import_logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"import_errors_{timestamp}.log")
            try:
                with open(log_file, 'w', encoding='utf-8') as f:
                    f.write(f"Batch Import Error Log\n")
                    f.write(f"Timestamp: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Total: {total}  Success: {success_count}  Failed: {fail_count}\n")
                    f.write(f"{'='*60}\n\n")
                    for mesh_path, reason in failed_meshes:
                        f.write(f"Mesh: {os.path.basename(mesh_path)}\n")
                        f.write(f"  Path:   {mesh_path}\n")
                        f.write(f"  Reason: {reason}\n\n")
                self.queue.put(('log', f"\n  Error log saved to: {log_file}"))
            except Exception as e:
                self.queue.put(('log', f"\n  ⚠ Failed to write error log: {e}"))

        self.queue.put(('log', f"\n{'='*60}"))
        self.queue.put(('log', f"Batch Import Complete: {success_count} succeeded, {fail_count} failed (of {total})"))
        self.queue.put(('log', f"{'='*60}"))
        self.queue.put(('done',))

    def _poll(self):
        try:
            while True:
                m = self.queue.get_nowait()
                if m[0] == 'progress':
                    _, cur, total, msg = m
                    self.pb['maximum'] = total
                    self.pb['value'] = cur
                    self.status_var.set(msg)
                elif m[0] == 'log':
                    self._log(m[1])
                elif m[0] == 'done':
                    self.status_var.set("Import complete!")
                    self.importing = False
                    self.import_btn.config(state='normal')
                    self.resolve_btn.config(state='normal')
                    self.batch_import_btn.config(state='normal')
                elif m[0] == 'error':
                    self._log(f"✗ ERROR: {m[1]}")
                    self.status_var.set(f"Error: {m[1]}")
                    self.importing = False
                    self.import_btn.config(state='normal')
                    self.resolve_btn.config(state='normal')
                    self.batch_import_btn.config(state='normal')
                elif m[0] == 'ue_refresh_done':
                    ok, msg = m[1], m[2]
                    self.refresh_ue_btn.config(state='normal')
                    if ok:
                        self.status_var.set(f"UE import status refreshed: {msg}")
                    else:
                        self.status_var.set(f"UE refresh failed: {msg}")
                    self._filter_tree()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # --- UI helpers ---

    def _log(self, msg):
        self.log_text.config(state='normal')
        upper = msg.upper()
        if 'ERROR' in upper or 'FAILED' in upper or '✗' in msg:
            self.log_text.insert('end', msg + '\n', 'error')
        elif 'WARN' in upper or 'SKIP' in upper or '⚠' in msg:
            self.log_text.insert('end', msg + '\n', 'warning')
        else:
            self.log_text.insert('end', msg + '\n')
        self.log_text.see('end')
        self.log_text.config(state='disabled')

    def _log_right_click(self, e):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='Clear Log', command=self._clear_log)
        menu.tk_popup(e.x_root, e.y_root)

    def _clear_log(self):
        self.log_text.config(state='normal')
        self.log_text.delete('1.0', 'end')
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