#!/usr/bin/env python3
"""Diagnose MI material texture parameters and connections in UE."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'Scripts'))
from ue_remote import UERemoteExec

MI_PATH = "/Game/Developers/bosonhuang/SlasherAsset/SLASHER/Art/Environment/Architecture/UnderdwellerV2/Floors_V2/MI_MB_FloorsV2_Tiles.MI_MB_FloorsV2_Tiles"

# Also check the material we created
CREATED_MI = "/Game/Developers/bosonhuang/SlasherAsset/SLASHER/Art/Environment/Architecture/UnderdwellerV2/Floors_V2/MI_MB_FloorsV2_Tiles"

client = UERemoteExec()
print("Connecting to UE...")
if not client.connect():
    print("Failed to connect to UE")
    sys.exit(1)
print("Connected!\n")

# 1. Read the original MI's texture parameter values
cmd = f'''
import unreal
mi = unreal.load_asset("{MI_PATH}")
if not mi:
    result = "ERROR: MI not found"
else:
    # Get all texture parameters
    tex_params = unreal.MaterialEditingLibrary.get_static_switch_parameters(mi)
    # Get texture parameter values
    info = []
    # Use MaterialInstanceDynamic or MaterialInstanceConstant API
    # Try getting texture parameter values via MaterialInstanceConstant
    try:
        # List all scalar, vector, texture parameters
        scalar_vals = unreal.MaterialEditingLibrary.get_scalar_parameter_values(mi)
        tex_vals = unreal.MaterialEditingLibrary.get_texture_parameter_values(mi)
        vector_vals = unreal.MaterialEditingLibrary.get_vector_parameter_values(mi)
        info.append(f"Scalar params: {{scalar_vals}}")
        info.append(f"Texture params: {{tex_vals}}")
        info.append(f"Vector params: {{vector_vals}}")
    except Exception as e:
        info.append(f"API error: {{e}}")
    
    # Try alternative: get material parameter info
    try:
        import unreal
        mat = mi
        # Get texture parameter values using the editor API
        param_info = []
        # Get all texture parameter names from the parent material
        parent = mat.get_base_material()
        parent_name = parent.get_name() if parent else "None"
        param_info.append(f"Parent material: {{parent_name}}")
        
        # Get texture parameter values from MI
        tex_params = mat.get_texture_parameter_values() if hasattr(mat, 'get_texture_parameter_values') else []
        for tp in tex_params:
            try:
                pname = tp.parameter_info.name if hasattr(tp, 'parameter_info') else str(tp)
                pval = tp.parameter_value.get_name() if tp.parameter_value else "None"
                param_info.append(f"  TexParam: {{pname}} = {{pval}}")
            except:
                param_info.append(f"  TexParam: {{str(tp)}}")
        result = "\\n".join(info + param_info)
    except Exception as e:
        result = "\\n".join(info + [f"Alt API error: {{e}}"])
'''
print("Querying MI texture parameters...")
r = client.run_command(cmd, mode='eval', timeout=30)
print(f"Result: {r.get('result', r.get('error', 'no result'))}")

# 2. Read the material we created - check its expressions
cmd2 = f'''
import unreal
mat_path = "{CREATED_MI}"
mat = unreal.load_asset(mat_path)
if not mat:
    result = "ERROR: Created material not found at " + mat_path
else:
    # Get all material expressions
    expressions = unreal.MaterialEditingLibrary.get_material_expression_collection(mat)
    info = []
    info.append(f"Material: {{mat.get_name()}}")
    info.append(f"Expression count: {{len(expressions) if expressions else 0}}")
    if expressions:
        for i, expr in enumerate(expressions):
            try:
                expr_type = type(expr).__name__
                # Try to get texture
                tex_name = ""
                if hasattr(expr, 'texture'):
                    tex = expr.get_editor_property("texture")
                    if tex:
                        tex_name = tex.get_name()
                # Try to get constant value
                const_val = ""
                if isinstance(expr, unreal.MaterialExpressionConstant):
                    const_val = str(expr.get_editor_property("R"))
                elif isinstance(expr, unreal.MaterialExpressionConstant3Vector):
                    const_val = str(expr.get_editor_property("Constant"))
                info.append(f"  [{{i}}] {{expr_type}}: tex={{tex_name}}, val={{const_val}}")
            except Exception as e:
                info.append(f"  [{{i}}] Error: {{e}}")
    result = "\\n".join(info)
'''
print("\nQuerying created material expressions...")
r2 = client.run_command(cmd2, mode='eval', timeout=30)
print(f"Result: {r2.get('result', r2.get('error', 'no result'))}")

# 3. Check what textures are actually assigned to each parameter in the MI
cmd3 = f'''
import unreal
mi = unreal.load_asset("{MI_PATH}")
if not mi:
    result = "ERROR: MI not found"
else:
    parent = mi.get_base_material()
    parent_name = parent.get_name() if parent else "None"
    
    # Walk up to find the true master
    master = parent
    chain = [mi.get_name()]
    while master:
        chain.append(master.get_name())
        next_parent = master.get_base_material()
        if next_parent and next_parent != master:
            master = next_parent
        else:
            break
    
    info = ["Material chain: " + " -> ".join(chain)]
    
    # Get texture parameter values from MI
    try:
        tex_values = mi.get_texture_parameter_values()
        for tv in tex_values:
            pname = tv.parameter_info.name
            tex_obj = tv.parameter_value
            tex_name = tex_obj.get_name() if tex_obj else "None"
            tex_path = tex_obj.get_path_name() if tex_obj else "None"
            info.append(f"  Param '{{pname}}' -> {{tex_name}} ({{tex_path}})")
    except Exception as e:
        info.append(f"get_texture_parameter_values error: {{e}}")
    
    # Also check the parent's texture parameters
    try:
        if parent:
            tex_values_p = parent.get_texture_parameter_values()
            for tv in tex_values_p:
                pname = tv.parameter_info.name
                tex_obj = tv.parameter_value
                tex_name = tex_obj.get_name() if tex_obj else "None"
                tex_path = tex_obj.get_path_name() if tex_obj else "None"
                info.append(f"  Parent param '{{pname}}' -> {{tex_name}} ({{tex_path}})")
    except Exception as e:
        info.append(f"Parent tex params error: {{e}}")
    
    result = "\\n".join(info)
'''
print("\nQuerying MI parameter -> texture mapping...")
r3 = client.run_command(cmd3, mode='eval', timeout=30)
print(f"Result: {r3.get('result', r3.get('error', 'no result'))}")

client.disconnect()
