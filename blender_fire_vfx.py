bl_info = {
    "name": "Fire VFX Rig (Mantaflow)",
    "author": "Cursor Agent",
    "version": (0, 2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Fire VFX",
    "description": "Create adjustable fire rigs (domain + emitter + shader) with presets.",
    "category": "Object",
}

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup


# -----------------------------
# Utilities
# -----------------------------

COLLECTION_NAME = "FireVFX"
DOMAIN_NAME = "FireVFX_Domain"
EMITTER_NAME = "FireVFX_Emitter"
MATERIAL_NAME = "FireVFX_Volume"


def _set_if_has(obj, attr, value):
    """Set attribute if it exists; swallow Blender version differences."""
    if obj is None:
        return
    if hasattr(obj, attr):
        try:
            setattr(obj, attr, value)
        except Exception:
            # Some RNA props can throw due to mode/context/range.
            pass


def _ensure_collection(name: str):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col


def _link_to_collection(obj, col):
    if obj is None or col is None:
        return
    # Unlink from current collections (except scene root) then link to ours.
    for c in list(obj.users_collection):
        try:
            c.objects.unlink(obj)
        except Exception:
            pass
    try:
        col.objects.link(obj)
    except Exception:
        pass


def _get_or_create_material(name: str):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    return mat


def _set_color_ramp_elements(color_ramp, elements):
    """Set ColorRamp elements deterministically.

    Blender doesn't allow removing the last element in some versions, so we:
    - Ensure at least 1 element exists
    - Resize to match desired count
    - Set positions/colors in index order
    """
    if color_ramp is None:
        return

    desired = list(elements)
    if not desired:
        return

    cr = color_ramp

    # Ensure at least one element exists.
    if len(cr.elements) == 0:
        cr.elements.new(0.0)

    # Grow/shrink to desired length.
    while len(cr.elements) < len(desired):
        cr.elements.new(desired[len(cr.elements)][0])

    while len(cr.elements) > len(desired):
        try:
            cr.elements.remove(cr.elements[-1])
        except Exception:
            break

    # Apply values.
    for i, (pos, col) in enumerate(desired):
        try:
            cr.elements[i].position = float(pos)
            cr.elements[i].color = col
        except Exception:
            pass


def _build_volume_shader(mat, flame_strength=25.0, smoke_density=2.0, flame_ramp=((0.0, (0.05, 0.01, 0.0, 1.0)), (0.25, (1.0, 0.35, 0.05, 1.0)), (1.0, (1.0, 1.0, 1.0, 1.0)))):
    """Build a simple, robust Mantaflow volume shader.

    Uses `Attribute` nodes: "density" and "flame".
    """

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (520, 0)

    pv = nodes.new("ShaderNodeVolumePrincipled")
    pv.location = (260, 0)

    # Density
    attr_density = nodes.new("ShaderNodeAttribute")
    attr_density.attribute_name = "density"
    attr_density.location = (-520, -120)

    # Flame
    attr_flame = nodes.new("ShaderNodeAttribute")
    attr_flame.attribute_name = "flame"
    attr_flame.location = (-520, 140)

    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.location = (-240, 140)
    _set_color_ramp_elements(ramp.color_ramp, flame_ramp)

    # Flame strength: flame * strength
    mult = nodes.new("ShaderNodeMath")
    mult.operation = "MULTIPLY"
    mult.inputs[1].default_value = float(flame_strength)
    mult.location = (-20, 140)

    # Smoke density: density * smoke_density
    mult_d = nodes.new("ShaderNodeMath")
    mult_d.operation = "MULTIPLY"
    mult_d.inputs[1].default_value = float(smoke_density)
    mult_d.location = (-20, -120)

    # Links
    links.new(pv.outputs[0], out.inputs[1])  # Volume -> Material Output Volume
    links.new(attr_flame.outputs.get("Fac"), ramp.inputs[0])
    links.new(ramp.outputs.get("Color"), pv.inputs.get("Emission Color"))
    links.new(attr_flame.outputs.get("Fac"), mult.inputs[0])
    links.new(mult.outputs.get("Value"), pv.inputs.get("Emission Strength"))
    links.new(attr_density.outputs.get("Fac"), mult_d.inputs[0])
    links.new(mult_d.outputs.get("Value"), pv.inputs.get("Density"))

    # A little extinction helps smoke read.
    _set_if_has(pv.inputs.get("Anisotropy"), "default_value", 0.2)
    _set_if_has(pv.inputs.get("Color"), "default_value", (0.2, 0.2, 0.2, 1.0))


def _ensure_domain_material(domain_obj, settings):
    if domain_obj is None:
        return
    mat = _get_or_create_material(MATERIAL_NAME)

    flame_ramp = (
        (0.0, settings.flame_color_low),
        (0.25, settings.flame_color_mid),
        (1.0, settings.flame_color_high),
    )
    _build_volume_shader(
        mat,
        flame_strength=settings.flame_strength,
        smoke_density=settings.smoke_density,
        flame_ramp=flame_ramp,
    )

    if domain_obj.data is not None:
        if len(domain_obj.data.materials) == 0:
            domain_obj.data.materials.append(mat)
        else:
            domain_obj.data.materials[0] = mat


def _ensure_fluid_modifier(obj):
    if obj is None:
        return None
    for m in obj.modifiers:
        if m.type == "FLUID":
            return m
    return obj.modifiers.new(name="Fluid", type="FLUID")


def _apply_domain_settings(domain_obj, settings):
    mod = _ensure_fluid_modifier(domain_obj)
    if mod is None:
        return

    _set_if_has(mod, "fluid_type", "DOMAIN")

    ds = getattr(mod, "domain_settings", None)
    if ds is None:
        return

    _set_if_has(ds, "domain_type", "GAS")

    # Cache
    _set_if_has(ds, "cache_directory", settings.cache_directory)
    _set_if_has(ds, "cache_type", "MODULAR")

    # Core quality / sim
    _set_if_has(ds, "resolution_max", int(settings.resolution_max))
    _set_if_has(ds, "time_scale", float(settings.time_scale))
    _set_if_has(ds, "vorticity", float(settings.vorticity))

    # Adaptive domain / padding
    _set_if_has(ds, "use_adaptive_domain", bool(settings.use_adaptive_domain))
    _set_if_has(ds, "additional_res", int(settings.adaptive_additional_res))
    _set_if_has(ds, "adapt_margin", int(settings.adaptive_margin))

    # Noise
    _set_if_has(ds, "use_noise", bool(settings.use_noise))
    _set_if_has(ds, "noise_strength", float(settings.noise_strength))
    _set_if_has(ds, "noise_scale", float(settings.noise_scale))

    # Dissolve smoke (optional)
    _set_if_has(ds, "use_dissolve_smoke", bool(settings.use_dissolve_smoke))
    _set_if_has(ds, "dissolve_speed", int(settings.dissolve_speed))

    # Flames & smoke from reaction (property names vary by Blender version)
    _set_if_has(ds, "use_reaction", True)
    _set_if_has(ds, "burning_rate", float(settings.burning_rate))
    _set_if_has(ds, "flame_smoke", float(settings.flame_smoke))


def _apply_flow_settings(emitter_obj, settings):
    mod = _ensure_fluid_modifier(emitter_obj)
    if mod is None:
        return

    _set_if_has(mod, "fluid_type", "FLOW")

    fs = getattr(mod, "flow_settings", None)
    if fs is None:
        return

    # Type/behavior
    # Blender enums differ; try FIRE then SMOKE.
    if hasattr(fs, "flow_type"):
        try:
            fs.flow_type = "FIRE"
        except Exception:
            try:
                fs.flow_type = "SMOKE"
            except Exception:
                pass

    _set_if_has(fs, "flow_behavior", "INFLOW")

    # How much stuff we add
    _set_if_has(fs, "density", float(settings.flow_density))
    _set_if_has(fs, "temperature", float(settings.flow_temperature))
    _set_if_has(fs, "fuel_amount", float(settings.flow_fuel))

    # Initial velocity can cause a nice torch look; keep optional.
    _set_if_has(fs, "use_initial_velocity", bool(settings.use_initial_velocity))
    _set_if_has(fs, "velocity_factor", float(settings.velocity_factor))


def _find_rig(context):
    scene = context.scene
    settings = scene.fire_vfx_settings
    dom = bpy.data.objects.get(settings.domain_object_name) if settings.domain_object_name else None
    emi = bpy.data.objects.get(settings.emitter_object_name) if settings.emitter_object_name else None
    return dom, emi


# -----------------------------
# Presets
# -----------------------------

BASE_PRESET = {
    # Lightweight defaults (fast preview). Presets override only what they need.
    "domain_size": (2.0, 2.0, 3.0),
    "emitter_scale": (0.07, 0.07, 0.25),
    "resolution_max": 96,
    "time_scale": 1.0,
    "vorticity": 0.7,
    "use_adaptive_domain": True,
    "adaptive_margin": 4,
    "adaptive_additional_res": 0,
    "use_noise": False,
    "noise_strength": 1.0,
    "noise_scale": 2.5,
    "use_dissolve_smoke": False,
    "dissolve_speed": 30,
    "burning_rate": 1.2,
    "flame_smoke": 0.18,
    "flow_density": 1.0,
    "flow_temperature": 1.25,
    "flow_fuel": 1.15,
    "use_initial_velocity": True,
    "velocity_factor": 1.2,
    "flame_strength": 35.0,
    "smoke_density": 2.0,
    "flame_color_low": (0.07, 0.01, 0.0, 1.0),
    "flame_color_mid": (1.0, 0.42, 0.08, 1.0),
    "flame_color_high": (1.0, 0.95, 0.85, 1.0),
}

PRESET_OVERRIDES = {
    "CANDLE": {
        "domain_size": (1.5, 1.5, 2.5),
        "emitter_scale": (0.04, 0.04, 0.10),
        "resolution_max": 80,
        "vorticity": 0.25,
        "use_noise": True,
        "noise_strength": 0.4,
        "noise_scale": 2.0,
        "burning_rate": 0.8,
        "flame_smoke": 0.05,
        "flow_density": 0.6,
        "flow_temperature": 1.0,
        "flow_fuel": 1.0,
        "flame_strength": 18.0,
        "smoke_density": 0.8,
        "use_dissolve_smoke": True,
        "dissolve_speed": 35,
        "use_initial_velocity": False,
        "velocity_factor": 1.0,
        "flame_color_mid": (1.0, 0.55, 0.12, 1.0),
        "flame_color_high": (1.0, 1.0, 1.0, 1.0),
    },
    "TORCH": {
        "resolution_max": 112,
        "vorticity": 0.8,
        "use_noise": True,
        "noise_strength": 1.0,
        "noise_scale": 2.5,
        "velocity_factor": 1.5,
    },
    "CAMPFIRE": {
        "domain_size": (4.0, 4.0, 4.0),
        "emitter_scale": (0.45, 0.45, 0.18),
        "resolution_max": 144,
        "vorticity": 1.5,
        "use_noise": True,
        "noise_strength": 1.6,
        "noise_scale": 3.0,
        "burning_rate": 1.6,
        "flame_smoke": 0.35,
        "flow_density": 1.2,
        "flow_temperature": 1.35,
        "flow_fuel": 1.3,
        "flame_strength": 55.0,
        "smoke_density": 3.0,
        "velocity_factor": 1.0,
        "flame_color_mid": (1.0, 0.38, 0.06, 1.0),
    },
    "BONFIRE": {
        "domain_size": (7.0, 7.0, 7.0),
        "emitter_scale": (0.9, 0.9, 0.35),
        "resolution_max": 160,
        "vorticity": 2.2,
        "use_noise": True,
        "noise_strength": 2.0,
        "noise_scale": 3.5,
        "burning_rate": 2.1,
        "flame_smoke": 0.55,
        "flow_density": 1.4,
        "flow_temperature": 1.5,
        "flow_fuel": 1.5,
        "flame_strength": 75.0,
        "smoke_density": 4.2,
        "velocity_factor": 1.2,
        "flame_color_mid": (1.0, 0.30, 0.05, 1.0),
        "flame_color_high": (1.0, 0.9, 0.8, 1.0),
    },
    "EXPLOSION": {
        "domain_size": (10.0, 10.0, 10.0),
        "emitter_scale": (0.6, 0.6, 0.6),
        "resolution_max": 176,
        "time_scale": 0.85,
        "vorticity": 3.0,
        "use_noise": True,
        "noise_strength": 3.0,
        "noise_scale": 4.0,
        "burning_rate": 3.0,
        "flame_smoke": 0.9,
        "flow_density": 2.0,
        "flow_temperature": 2.0,
        "flow_fuel": 2.0,
        "flame_strength": 120.0,
        "smoke_density": 6.0,
        "use_dissolve_smoke": True,
        "dissolve_speed": 18,
        "velocity_factor": 2.0,
        "flame_color_low": (0.10, 0.02, 0.0, 1.0),
        "flame_color_mid": (1.0, 0.22, 0.03, 1.0),
        "flame_color_high": (1.0, 0.85, 0.65, 1.0),
    },
}


def _apply_preset_to_settings(settings, preset_id: str):
    # Apply base first for stable diffs and predictable behavior.
    for k, v in BASE_PRESET.items():
        if hasattr(settings, k):
            try:
                setattr(settings, k, v)
            except Exception:
                pass

    overrides = PRESET_OVERRIDES.get(preset_id, {})
    for k, v in overrides.items():
        if hasattr(settings, k):
            try:
                setattr(settings, k, v)
            except Exception:
                pass


def _preset_update(self, context):
    _apply_preset_to_settings(self, self.preset)


# -----------------------------
# Properties
# -----------------------------

class FIREVFX_Settings(PropertyGroup):
    preset: EnumProperty(
        name="Preset",
        items=[
            ("CANDLE", "Candle", "Small, calm flame"),
            ("TORCH", "Torch", "Taller, turbulent flame"),
            ("CAMPFIRE", "Campfire", "Medium fire, more smoke"),
            ("BONFIRE", "Bonfire", "Large, very turbulent"),
            ("EXPLOSION", "Explosion", "Fast, aggressive fireball"),
        ],
        default="TORCH",
        update=_preset_update,
    )

    # Rig object references stored by name (simple + robust)
    domain_object_name: StringProperty(name="Domain Object", default="")
    emitter_object_name: StringProperty(name="Emitter Object", default="")

    ui_show_advanced: BoolProperty(
        name="Show Advanced",
        default=False,
        description="Show extra controls (keeps panel lightweight by default).",
    )

    apply_scale_on_update: BoolProperty(
        name="Apply Scale",
        default=False,
        description="Apply object scale when creating/updating the rig (can be disruptive to selection).",
    )

    # Transform-ish
    domain_size: FloatVectorProperty(
        name="Domain Size",
        subtype="XYZ",
        size=3,
        min=0.1,
        default=(2.0, 2.0, 3.0),
        description="Size of the simulation domain in meters (approx).",
    )

    emitter_scale: FloatVectorProperty(
        name="Emitter Scale",
        subtype="XYZ",
        size=3,
        min=0.001,
        default=(0.07, 0.07, 0.25),
        description="Scale of the emitter object.",
    )

    # Domain quality & behavior
    resolution_max: IntProperty(name="Resolution", min=16, max=1024, default=96)
    time_scale: FloatProperty(name="Time Scale", min=0.05, max=3.0, default=1.0)
    vorticity: FloatProperty(name="Vorticity", min=0.0, max=10.0, default=0.8)

    use_adaptive_domain: BoolProperty(name="Adaptive Domain", default=True)
    adaptive_margin: IntProperty(name="Adaptive Margin", min=0, max=50, default=4)
    adaptive_additional_res: IntProperty(name="Adaptive Additional Res", min=0, max=256, default=0)

    # Noise
    use_noise: BoolProperty(name="Noise", default=False)
    noise_strength: FloatProperty(name="Noise Strength", min=0.0, max=10.0, default=1.0)
    noise_scale: FloatProperty(name="Noise Scale", min=0.1, max=20.0, default=2.5)

    # Smoke handling
    use_dissolve_smoke: BoolProperty(name="Dissolve Smoke", default=False)
    dissolve_speed: IntProperty(name="Dissolve Speed", min=1, max=100, default=30)

    # Reaction-ish (property names differ across versions; applied if present)
    burning_rate: FloatProperty(name="Burning Rate", min=0.0, max=10.0, default=1.2)
    flame_smoke: FloatProperty(name="Flame→Smoke", min=0.0, max=5.0, default=0.18)

    # Flow
    flow_density: FloatProperty(name="Flow Density", min=0.0, max=10.0, default=1.0)
    flow_temperature: FloatProperty(name="Flow Temperature", min=0.0, max=10.0, default=1.25)
    flow_fuel: FloatProperty(name="Flow Fuel", min=0.0, max=10.0, default=1.15)

    use_initial_velocity: BoolProperty(name="Initial Velocity", default=True)
    velocity_factor: FloatProperty(name="Velocity Factor", min=0.0, max=10.0, default=1.5)

    # Shading
    flame_strength: FloatProperty(name="Flame Strength", min=0.0, max=500.0, default=35.0)
    smoke_density: FloatProperty(name="Smoke Density Mult", min=0.0, max=50.0, default=2.0)
    flame_color_low: FloatVectorProperty(
        name="Flame Low",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(0.07, 0.01, 0.0, 1.0),
    )
    flame_color_mid: FloatVectorProperty(
        name="Flame Mid",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(1.0, 0.42, 0.08, 1.0),
    )
    flame_color_high: FloatVectorProperty(
        name="Flame High",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(1.0, 0.95, 0.85, 1.0),
    )

    # Cache path
    cache_directory: StringProperty(
        name="Cache Directory",
        subtype="DIR_PATH",
        default="//fire_vfx_cache",
        description="Relative to the .blend when starting with //",
    )


# -----------------------------
# Operators
# -----------------------------

class FIREVFX_OT_create_rig(Operator):
    bl_idname = "fire_vfx.create_rig"
    bl_label = "Create Fire Rig"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        settings = scene.fire_vfx_settings

        # Create collection
        col = _ensure_collection(COLLECTION_NAME)

        # Create (or reuse) objects
        domain = bpy.data.objects.get(DOMAIN_NAME)
        emitter = bpy.data.objects.get(EMITTER_NAME)

        if domain is None:
            bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 1.0))
            domain = context.active_object
            domain.name = DOMAIN_NAME

        if emitter is None:
            bpy.ops.mesh.primitive_cylinder_add(radius=0.25, depth=1.0, location=(0.0, 0.0, 0.25))
            emitter = context.active_object
            emitter.name = EMITTER_NAME

        # Link to collection
        _link_to_collection(domain, col)
        _link_to_collection(emitter, col)

        # Apply sizes
        domain.scale = (settings.domain_size[0] / 2.0, settings.domain_size[1] / 2.0, settings.domain_size[2] / 2.0)
        emitter.scale = settings.emitter_scale

        # Optional (can be disruptive to selection); off by default for lightweightness.
        if settings.apply_scale_on_update:
            try:
                bpy.context.view_layer.objects.active = domain
                domain.select_set(True)
                bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            except Exception:
                pass
            try:
                bpy.context.view_layer.objects.active = emitter
                emitter.select_set(True)
                bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            except Exception:
                pass

        # Fluid settings
        _apply_domain_settings(domain, settings)
        _apply_flow_settings(emitter, settings)

        # Shader
        _ensure_domain_material(domain, settings)

        # Store references
        settings.domain_object_name = domain.name
        settings.emitter_object_name = emitter.name

        # Small viewport niceties
        _set_if_has(domain, "display_type", "WIRE")
        _set_if_has(emitter, "display_type", "SOLID")

        return {"FINISHED"}


class FIREVFX_OT_update_rig(Operator):
    bl_idname = "fire_vfx.update_rig"
    bl_label = "Update Rig From Settings"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        settings = scene.fire_vfx_settings
        domain, emitter = _find_rig(context)

        if domain is None or emitter is None:
            self.report({"WARNING"}, "No rig found. Click Create Fire Rig first.")
            return {"CANCELLED"}

        # Update transforms
        domain.scale = (settings.domain_size[0] / 2.0, settings.domain_size[1] / 2.0, settings.domain_size[2] / 2.0)
        emitter.scale = settings.emitter_scale

        if settings.apply_scale_on_update:
            # Apply transforms (scale only) to avoid changing sim location.
            try:
                bpy.context.view_layer.objects.active = domain
                domain.select_set(True)
                bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            except Exception:
                pass
            try:
                bpy.context.view_layer.objects.active = emitter
                emitter.select_set(True)
                bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            except Exception:
                pass

        _apply_domain_settings(domain, settings)
        _apply_flow_settings(emitter, settings)
        _ensure_domain_material(domain, settings)

        return {"FINISHED"}


class FIREVFX_OT_bake_all(Operator):
    bl_idname = "fire_vfx.bake_all"
    bl_label = "Bake (All)"

    def execute(self, context):
        domain, _emitter = _find_rig(context)
        if domain is None:
            self.report({"WARNING"}, "No domain found. Create the rig first.")
            return {"CANCELLED"}

        # Ensure domain is active for bake ops.
        try:
            bpy.ops.object.select_all(action="DESELECT")
            domain.select_set(True)
            context.view_layer.objects.active = domain
        except Exception:
            pass

        # Bake API differs; try common ops.
        try:
            bpy.ops.fluid.bake_all()
        except Exception:
            try:
                bpy.ops.fluid.free_all()
                bpy.ops.fluid.bake_all()
            except Exception as e:
                self.report({"ERROR"}, f"Bake failed: {e}")
                return {"CANCELLED"}

        return {"FINISHED"}


class FIREVFX_OT_free_all(Operator):
    bl_idname = "fire_vfx.free_all"
    bl_label = "Free Bake"

    def execute(self, context):
        domain, _emitter = _find_rig(context)
        if domain is None:
            self.report({"WARNING"}, "No domain found. Create the rig first.")
            return {"CANCELLED"}

        try:
            bpy.ops.object.select_all(action="DESELECT")
            domain.select_set(True)
            context.view_layer.objects.active = domain
        except Exception:
            pass

        try:
            bpy.ops.fluid.free_all()
        except Exception as e:
            self.report({"ERROR"}, f"Free failed: {e}")
            return {"CANCELLED"}

        return {"FINISHED"}


# -----------------------------
# UI
# -----------------------------

class FIREVFX_PT_panel(Panel):
    bl_label = "Fire VFX"
    bl_idname = "FIREVFX_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Fire VFX"

    def draw(self, context):
        layout = self.layout
        s = context.scene.fire_vfx_settings

        col = layout.column(align=True)
        col.prop(s, "preset")
        row = col.row(align=True)
        row.operator("fire_vfx.create_rig", text="Create")
        row.operator("fire_vfx.update_rig", text="Update")
        col.prop(s, "ui_show_advanced")

        layout.separator()

        box = layout.box()
        box.label(text="Rig")
        box.prop(s, "domain_object_name")
        box.prop(s, "emitter_object_name")

        box = layout.box()
        box.label(text="Transforms")
        box.prop(s, "domain_size")
        box.prop(s, "emitter_scale")

        box = layout.box()
        box.label(text="Simulation")
        box.prop(s, "resolution_max")
        box.prop(s, "time_scale")
        box.prop(s, "vorticity")

        row = box.row(align=True)
        row.prop(s, "use_adaptive_domain")
        if s.use_adaptive_domain:
            box.prop(s, "adaptive_margin")
            box.prop(s, "adaptive_additional_res")

        if s.ui_show_advanced:
            row = box.row(align=True)
            row.prop(s, "use_noise")
            if s.use_noise:
                box.prop(s, "noise_strength")
                box.prop(s, "noise_scale")

            box.prop(s, "burning_rate")
            box.prop(s, "flame_smoke")

            row = box.row(align=True)
            row.prop(s, "use_dissolve_smoke")
            if s.use_dissolve_smoke:
                box.prop(s, "dissolve_speed")

        box = layout.box()
        box.label(text="Flow")
        box.prop(s, "flow_density")
        box.prop(s, "flow_temperature")
        box.prop(s, "flow_fuel")
        if s.ui_show_advanced:
            row = box.row(align=True)
            row.prop(s, "use_initial_velocity")
            if s.use_initial_velocity:
                box.prop(s, "velocity_factor")

        box = layout.box()
        box.label(text="Shading")
        box.prop(s, "flame_strength")
        box.prop(s, "smoke_density")
        box.prop(s, "flame_color_low")
        box.prop(s, "flame_color_mid")
        box.prop(s, "flame_color_high")

        box = layout.box()
        box.label(text="Cache / Bake")
        box.prop(s, "cache_directory")
        if s.ui_show_advanced:
            box.prop(s, "apply_scale_on_update")
        row = box.row(align=True)
        row.operator("fire_vfx.bake_all", text="Bake")
        row.operator("fire_vfx.free_all", text="Free")


# -----------------------------
# Registration
# -----------------------------

CLASSES = (
    FIREVFX_Settings,
    FIREVFX_OT_create_rig,
    FIREVFX_OT_update_rig,
    FIREVFX_OT_bake_all,
    FIREVFX_OT_free_all,
    FIREVFX_PT_panel,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.fire_vfx_settings = PointerProperty(type=FIREVFX_Settings)

    # Initialize defaults from preset once.
    s = bpy.context.scene.fire_vfx_settings
    _apply_preset_to_settings(s, s.preset)


def unregister():
    if hasattr(bpy.types.Scene, "fire_vfx_settings"):
        del bpy.types.Scene.fire_vfx_settings
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
