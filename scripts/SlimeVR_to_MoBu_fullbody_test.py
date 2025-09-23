# -*- coding: utf-8 -*-

from pythonosc import dispatcher, osc_server
import threading
from pyfbsdk import (
    FBSystem,
    FBModelSkeleton,
    FBVector3d,
    FBVector4d,
    FBQuaternionToRotation,
    FBRotationOrder,
    FBModelTransformationType
)

VMC_IP   = "127.0.0.1"
VMC_PORT = 39539
UNIT     = 100.0

bone_map = {}     # VMCname→FBModelSkeletonname
_latest = {}
_lock = threading.Lock()
_linked = False   

# --- VMCnemame → MoBuname mapping ---
NAME_MAP = {
    "root": "root",
    "Hips": "Hips",
    "Spine": "Spine",
    "Chest": "Chest",
    "UpperChest": "UpChest", 
    "Neck": "Neck",
    "Head": "Head",

    "LeftShoulder": "RightShoulder",
    "LeftUpperArm": "RightArm",
    "LeftLowerArm": "RightForeArm",
    "LeftHand": "RightHand",

    "RightShoulder": "LeftShoulder",
    "RightUpperArm": "LeftArm",
    "RightLowerArm": "LeftForeArm",
    "RightHand": "LeftHand",

    "LeftUpperLeg": "RightUpLeg",
    "LeftLowerLeg": "RightLeg",
    "LeftFoot": "RightFoot",

    "RightUpperLeg": "LeftUpLeg",
    "RightLowerLeg": "LeftLeg",
    "RightFoot": "LeftFoot",
}

# --- （VMCbone）---
BONE_HIERARCHY = {
    "root": None,
    "Hips": "root",
    "Spine": "Hips",
    "Chest": "Spine",
    "UpperChest": "Chest",
    "Neck": "UpperChest",
    "Head": "Neck",

    "LeftShoulder": "UpperChest",
    "LeftUpperArm": "LeftShoulder",
    "LeftLowerArm": "LeftUpperArm",
    "LeftHand": "LeftLowerArm",

    "RightShoulder": "UpperChest",
    "RightUpperArm": "RightShoulder",
    "RightLowerArm": "RightUpperArm",
    "RightHand": "RightLowerArm",

    "LeftUpperLeg": "Hips",
    "LeftLowerLeg": "LeftUpperLeg",
    "LeftFoot": "LeftLowerLeg",

    "RightUpperLeg": "Hips",
    "RightLowerLeg": "RightUpperLeg",
    "RightFoot": "RightLowerLeg"
}

# --- buildbones ---
def get_or_create_bone(vmc_name, init_pos=None):
    if vmc_name in bone_map:
        return bone_map[vmc_name]

    mobu_name = NAME_MAP.get(vmc_name, vmc_name)  # mapping
    bone = FBModelSkeleton(mobu_name)
    bone.Show = True

    if init_pos is not None:
        bone.SetVector(
            FBVector3d(init_pos[0]*UNIT, init_pos[1]*UNIT, init_pos[2]*UNIT),
            FBModelTransformationType.kModelTranslation,
            True
        )

    bone_map[vmc_name] = bone
    print(f"[CREATE] {vmc_name} -> {mobu_name}")
    return bone

# --- linkbones ---
def link_bones():
    global _linked
    if _linked:
        return
    for vmc_name, parent_name in BONE_HIERARCHY.items():
        if vmc_name not in bone_map:
            continue
        if parent_name and parent_name in bone_map:
            bone_map[vmc_name].Parent = bone_map[parent_name]
            print(f"[LINK] {NAME_MAP.get(vmc_name, vmc_name)} -> {NAME_MAP.get(parent_name, parent_name)}")
    _linked = True

# --- OSC ---
def on_bone_pos(address, *args):
    try:
        bone_name = args[0]
        x, y, z = args[1:4]
        qx, qy, qz, qw = args[4:8]
    except Exception:
        return
    with _lock:
        _latest[bone_name] = (x, y, z, qx, qy, qz, qw)

# --- UI Idle ---
def _on_ui_idle(control, event):
    with _lock:
        items = list(_latest.items())
        _latest.clear()
    if not items:
        return

    for bone_name, (x, y, z, qx, qy, qz, qw) in items:
        mobu_bone = get_or_create_bone(bone_name, (x,y,z))
        if not mobu_bone:
            continue

        # pos
        mobu_bone.Translation = FBVector3d(x * UNIT, y * UNIT, z * UNIT)

        # rot
        euler = FBVector3d()
        quat  = FBVector4d(qx, qy, qz, qw)
        FBQuaternionToRotation(euler, quat, FBRotationOrder.kFBXYZ)
        mobu_bone.Rotation = euler

    # link firsttime
    link_bones()

# --- server ---
def start_server():
    disp = dispatcher.Dispatcher()
    disp.map("/VMC/Ext/Bone/Pos", on_bone_pos)
    server = osc_server.ThreadingOSCUDPServer((VMC_IP, VMC_PORT), disp)
    print(f"[VMC OSC] Listening on {VMC_IP}:{VMC_PORT}")
    server.serve_forever()

FBSystem().OnUIIdle.Add(_on_ui_idle)
threading.Thread(target=start_server, daemon=True).start()
print("[VMC] Skeleton_Linked!")


