# -*- coding: utf-8 -*-
from pythonosc import dispatcher, osc_server
import threading
import pyfbsdk

VMC_IP   = "127.0.0.1"
VMC_PORT = 39539
UNIT     = 100.0

bone_map = {}
_latest = {}
_lock = threading.Lock()

BONE_HIERARCHY = {
  # "root": None,
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
    "RightFoot": "RightLowerLeg",
}

def get_or_create_bone(vmc_name):
    if vmc_name in bone_map:
        return bone_map[vmc_name]
    bone = pyfbsdk.FBModelSkeleton(vmc_name)
    bone.Show = True
    parent_name = BONE_HIERARCHY.get(vmc_name)
    if parent_name:
        parent = get_or_create_bone(parent_name)
        bone.Parent = parent
    bone_map[vmc_name] = bone
    return bone

 def on_bone_pos(address, *args):
#    if  bone_name == "Neck":
#        bone_name = "Head"  

    try:
        bone_name = args[0]
        x, y, z = args[1:4]
        qx, qy, qz, qw = args[4:8]
    except Exception:
        return
    with _lock:
        _latest[bone_name] = (x, y, z, qx, qy, qz, qw)


def _on_ui_idle(control, event):
    with _lock:
        items = list(_latest.items())
        _latest.clear()
    if not items:
        return

    for bone_name, (x, y, z, qx, qy, qz, qw) in items:
        mobu_bone = get_or_create_bone(bone_name)
        # pos
        mobu_bone.Translation = pyfbsdk.FBVector3d(x*UNIT, y*UNIT, z*UNIT)
        # rot
        euler = pyfbsdk.FBVector3d()
        quat  = pyfbsdk.FBVector4d(qx, qy, qz, qw)
        pyfbsdk.FBQuaternionToRotation(euler, quat, pyfbsdk.FBRotationOrder.kFBXYZ)
        mobu_bone.Rotation = euler

def start_server():
    disp = dispatcher.Dispatcher()
    disp.map("/VMC/Ext/Bone/Pos", on_bone_pos)
    server = osc_server.ThreadingOSCUDPServer((VMC_IP, VMC_PORT), disp)
    print(f"[VMC OSC] Listening on {VMC_IP}:{VMC_PORT}")
    server.serve_forever()

# MotionBuilder  UI 
pyfbsdk.FBSystem().OnUIIdle.Add(_on_ui_idle)
threading.Thread(target=start_server, daemon=True).start()
print("[VMC] Start_VMC")


