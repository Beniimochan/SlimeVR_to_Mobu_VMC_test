"""
MotionBuilder 2025 - VMC (Virtual Motion Capture) Protocol Receiver Plugin
==========================================================================
ドラッグ＆ドロップでMotionBuilder上に起動し、VMCプロトコル（mocopi / VSeeFace等）を受信して
スケルトンの自動生成および自動キャラクタライズ（FBCharacter）を行うプラグインです。
外部依存なし（MotionBuilder内蔵の標準ライブラリおよびPySide6のみで動作）。
"""

import math
import socket
import struct
import sys
import threading
import time

# MotionBuilder pyfbsdk import (フォールバック対応)
try:
    import pyfbsdk as fb
    from pyfbsdk import (
        FBSystem,
        FBModelSkeleton,
        FBModelNull,
        FBCharacter,
        FBVector3d,
        FBVector4d,
        FBColor,
        FBFindObjectByFullName,
        FBBeginChangeAllModels,
        FBEndChangeAllModels,
        FBQuaternionToRotation,
    )
    IN_MOTIONBUILDER = True
except ImportError:
    IN_MOTIONBUILDER = False
    fb = None

# PySide6 & Shiboken6 import
try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import wrapInstance, getCppPointer
except ImportError:
    try:
        from PySide2 import QtWidgets, QtCore, QtGui
        from shiboken2 import wrapInstance, getCppPointer
    except ImportError:
        wrapInstance = None
        getCppPointer = None

# -----------------------------------------------------------------------------
# OSC Parser (Pure Python, 外部依存なし)
# -----------------------------------------------------------------------------
def _pad4(length):
    return (4 - (length % 4)) % 4

def _read_osc_string(data, offset):
    end = data.find(b'\x00', offset)
    if end == -1:
        raise ValueError("Unterminated OSC string")
    s = data[offset:end].decode('utf-8', errors='replace')
    pad = 4 - ((end - offset + 1) % 4)
    if pad == 4:
        pad = 0
    next_offset = end + 1 + pad
    return s, next_offset

def parse_osc_message(data, offset=0):
    address, offset = _read_osc_string(data, offset)
    if not address.startswith('/'):
        return None, offset
    
    if offset >= len(data):
        return (address, []), offset
    
    if data[offset:offset+1] == b',':
        typetags, offset = _read_osc_string(data, offset)
        typetags = typetags[1:] # 最初のコンマを除去
    else:
        typetags = ""
    
    args = []
    for tag in typetags:
        if tag == 'f':
            val = struct.unpack_from('>f', data, offset)[0]
            offset += 4
            args.append(val)
        elif tag == 'i':
            val = struct.unpack_from('>i', data, offset)[0]
            offset += 4
            args.append(val)
        elif tag == 's':
            val, offset = _read_osc_string(data, offset)
            args.append(val)
        elif tag == 'b':
            blob_len = struct.unpack_from('>i', data, offset)[0]
            offset += 4
            val = data[offset:offset+blob_len]
            offset += blob_len + _pad4(blob_len)
            args.append(val)
        elif tag == 'd':
            val = struct.unpack_from('>d', data, offset)[0]
            offset += 8
            args.append(val)
        elif tag == 'h':
            val = struct.unpack_from('>q', data, offset)[0]
            offset += 8
            args.append(val)
        elif tag == 'T':
            args.append(True)
        elif tag == 'F':
            args.append(False)
        elif tag == 'N':
            args.append(None)
    return (address, args), offset

def parse_osc_packet(data, offset=0):
    messages = []
    if data.startswith(b'#bundle\x00', offset):
        offset += 8
        if offset + 8 > len(data):
            return messages
        timetag = struct.unpack_from('>Q', data, offset)[0]
        offset += 8
        while offset + 4 <= len(data):
            elem_size = struct.unpack_from('>i', data, offset)[0]
            offset += 4
            if offset + elem_size > len(data):
                break
            elem_data = data[offset:offset+elem_size]
            offset += elem_size
            sub_msgs = parse_osc_packet(elem_data, 0)
            messages.extend(sub_msgs)
    elif data.startswith(b'/', offset):
        msg, _ = parse_osc_message(data, offset)
        if msg:
            messages.append(msg)
    return messages


# -----------------------------------------------------------------------------
# スケルトン定義＆キャラクタライズマッピング (VRM Humanoid 準拠)
# -----------------------------------------------------------------------------
SKELETON_DEF = {
    "Root": {
        "parent": None,
        "pos": (0.0, 0.0, 0.0),
        "is_null": True,
        "slot": "ReferenceLink"
    },
    "Hips": {
        "parent": "Root",
        "pos": (0.0, 100.0, 0.0),
        "slot": "HipsLink"
    },
    "Spine": {
        "parent": "Hips",
        "pos": (0.0, 15.0, 0.0),
        "slot": "SpineLink"
    },
    "Chest": {
        "parent": "Spine",
        "pos": (0.0, 15.0, 0.0),
        "slot": "Spine1Link"
    },
    "UpperChest": {
        "parent": "Chest",
        "pos": (0.0, 15.0, 0.0),
        "slot": "Spine2Link"
    },
    "Neck": {
        "parent": "UpperChest",
        "pos": (0.0, 10.0, 0.0),
        "slot": "NeckLink"
    },
    "Head": {
        "parent": "Neck",
        "pos": (0.0, 12.0, 0.0),
        "slot": "HeadLink"
    },
    # 左腕 (+X 方向へ水平展開)
    "LeftShoulder": {
        "parent": "UpperChest",
        "pos": (8.0, 0.0, 0.0),
        "slot": "LeftShoulderLink"
    },
    "LeftUpperArm": {
        "parent": "LeftShoulder",
        "pos": (12.0, 0.0, 0.0),
        "slot": "LeftArmLink"
    },
    "LeftLowerArm": {
        "parent": "LeftUpperArm",
        "pos": (25.0, 0.0, 0.0),
        "slot": "LeftForeArmLink"
    },
    "LeftHand": {
        "parent": "LeftLowerArm",
        "pos": (25.0, 0.0, 0.0),
        "slot": "LeftHandLink"
    },
    # 右腕 (-X 方向へ水平展開)
    "RightShoulder": {
        "parent": "UpperChest",
        "pos": (-8.0, 0.0, 0.0),
        "slot": "RightShoulderLink"
    },
    "RightUpperArm": {
        "parent": "RightShoulder",
        "pos": (-12.0, 0.0, 0.0),
        "slot": "RightArmLink"
    },
    "RightLowerArm": {
        "parent": "RightUpperArm",
        "pos": (-25.0, 0.0, 0.0),
        "slot": "RightForeArmLink"
    },
    "RightHand": {
        "parent": "RightLowerArm",
        "pos": (-25.0, 0.0, 0.0),
        "slot": "RightHandLink"
    },
    # 左脚 (-Y 方向)
    "LeftUpperLeg": {
        "parent": "Hips",
        "pos": (10.0, -5.0, 0.0),
        "slot": "LeftUpLegLink"
    },
    "LeftLowerLeg": {
        "parent": "LeftUpperLeg",
        "pos": (0.0, -45.0, 0.0),
        "slot": "LeftLegLink"
    },
    "LeftFoot": {
        "parent": "LeftLowerLeg",
        "pos": (0.0, -42.0, 0.0),
        "slot": "LeftFootLink"
    },
    "LeftToes": {
        "parent": "LeftFoot",
        "pos": (0.0, -8.0, 15.0),
        "slot": "LeftToeBaseLink"
    },
    # 右脚 (-Y 方向)
    "RightUpperLeg": {
        "parent": "Hips",
        "pos": (-10.0, -5.0, 0.0),
        "slot": "RightUpLegLink"
    },
    "RightLowerLeg": {
        "parent": "RightUpperLeg",
        "pos": (0.0, -45.0, 0.0),
        "slot": "RightLegLink"
    },
    "RightFoot": {
        "parent": "RightLowerLeg",
        "pos": (0.0, -42.0, 0.0),
        "slot": "RightFootLink"
    },
    "RightToes": {
        "parent": "RightFoot",
        "pos": (0.0, -8.0, 15.0),
        "slot": "RightToeBaseLink"
    },
    # 左手指 (Left Hand Fingers)
    "LeftThumbProximal": {
        "parent": "LeftHand",
        "pos": (3.0, -1.0, 2.5),
        "slot": "LeftHandThumb1Link"
    },
    "LeftThumbIntermediate": {
        "parent": "LeftThumbProximal",
        "pos": (2.5, -0.5, 1.0),
        "slot": "LeftHandThumb2Link"
    },
    "LeftThumbDistal": {
        "parent": "LeftThumbIntermediate",
        "pos": (2.0, -0.5, 0.5),
        "slot": "LeftHandThumb3Link"
    },
    "LeftIndexProximal": {
        "parent": "LeftHand",
        "pos": (7.0, 0.0, 2.0),
        "slot": "LeftHandIndex1Link"
    },
    "LeftIndexIntermediate": {
        "parent": "LeftIndexProximal",
        "pos": (3.5, 0.0, 0.0),
        "slot": "LeftHandIndex2Link"
    },
    "LeftIndexDistal": {
        "parent": "LeftIndexIntermediate",
        "pos": (2.5, 0.0, 0.0),
        "slot": "LeftHandIndex3Link"
    },
    "LeftMiddleProximal": {
        "parent": "LeftHand",
        "pos": (7.5, 0.0, 0.5),
        "slot": "LeftHandMiddle1Link"
    },
    "LeftMiddleIntermediate": {
        "parent": "LeftMiddleProximal",
        "pos": (4.0, 0.0, 0.0),
        "slot": "LeftHandMiddle2Link"
    },
    "LeftMiddleDistal": {
        "parent": "LeftMiddleIntermediate",
        "pos": (3.0, 0.0, 0.0),
        "slot": "LeftHandMiddle3Link"
    },
    "LeftRingProximal": {
        "parent": "LeftHand",
        "pos": (7.0, 0.0, -1.0),
        "slot": "LeftHandRing1Link"
    },
    "LeftRingIntermediate": {
        "parent": "LeftRingProximal",
        "pos": (3.5, 0.0, 0.0),
        "slot": "LeftHandRing2Link"
    },
    "LeftRingDistal": {
        "parent": "LeftRingIntermediate",
        "pos": (2.5, 0.0, 0.0),
        "slot": "LeftHandRing3Link"
    },
    "LeftLittleProximal": {
        "parent": "LeftHand",
        "pos": (6.5, 0.0, -2.5),
        "slot": "LeftHandPinky1Link"
    },
    "LeftLittleIntermediate": {
        "parent": "LeftLittleProximal",
        "pos": (3.0, 0.0, 0.0),
        "slot": "LeftHandPinky2Link"
    },
    "LeftLittleDistal": {
        "parent": "LeftLittleIntermediate",
        "pos": (2.0, 0.0, 0.0),
        "slot": "LeftHandPinky3Link"
    },
    # 右手指 (Right Hand Fingers)
    "RightThumbProximal": {
        "parent": "RightHand",
        "pos": (-3.0, -1.0, 2.5),
        "slot": "RightHandThumb1Link"
    },
    "RightThumbIntermediate": {
        "parent": "RightThumbProximal",
        "pos": (-2.5, -0.5, 1.0),
        "slot": "RightHandThumb2Link"
    },
    "RightThumbDistal": {
        "parent": "RightThumbIntermediate",
        "pos": (-2.0, -0.5, 0.5),
        "slot": "RightHandThumb3Link"
    },
    "RightIndexProximal": {
        "parent": "RightHand",
        "pos": (-7.0, 0.0, 2.0),
        "slot": "RightHandIndex1Link"
    },
    "RightIndexIntermediate": {
        "parent": "RightIndexProximal",
        "pos": (-3.5, 0.0, 0.0),
        "slot": "RightHandIndex2Link"
    },
    "RightIndexDistal": {
        "parent": "RightIndexIntermediate",
        "pos": (-2.5, 0.0, 0.0),
        "slot": "RightHandIndex3Link"
    },
    "RightMiddleProximal": {
        "parent": "RightHand",
        "pos": (-7.5, 0.0, 0.5),
        "slot": "RightHandMiddle1Link"
    },
    "RightMiddleIntermediate": {
        "parent": "RightMiddleProximal",
        "pos": (-4.0, 0.0, 0.0),
        "slot": "RightHandMiddle2Link"
    },
    "RightMiddleDistal": {
        "parent": "RightMiddleIntermediate",
        "pos": (-3.0, 0.0, 0.0),
        "slot": "RightHandMiddle3Link"
    },
    "RightRingProximal": {
        "parent": "RightHand",
        "pos": (-7.0, 0.0, -1.0),
        "slot": "RightHandRing1Link"
    },
    "RightRingIntermediate": {
        "parent": "RightRingProximal",
        "pos": (-3.5, 0.0, 0.0),
        "slot": "RightHandRing2Link"
    },
    "RightRingDistal": {
        "parent": "RightRingIntermediate",
        "pos": (-2.5, 0.0, 0.0),
        "slot": "RightHandRing3Link"
    },
    "RightLittleProximal": {
        "parent": "RightHand",
        "pos": (-6.5, 0.0, -2.5),
        "slot": "RightHandPinky1Link"
    },
    "RightLittleIntermediate": {
        "parent": "RightLittleProximal",
        "pos": (-3.0, 0.0, 0.0),
        "slot": "RightHandPinky2Link"
    },
    "RightLittleDistal": {
        "parent": "RightLittleIntermediate",
        "pos": (-2.0, 0.0, 0.0),
        "slot": "RightHandPinky3Link"
    },
}

BONE_ALIASES = {
    "waist": "Spine",
    "leftpinkyproximal": "LeftLittleProximal",
    "leftpinkyintermediate": "LeftLittleIntermediate",
    "leftpinkydistal": "LeftLittleDistal",
    "rightpinkyproximal": "RightLittleProximal",
    "rightpinkyintermediate": "RightLittleIntermediate",
    "rightpinkydistal": "RightLittleDistal",
    "lefttoe": "LeftToes",
    "righttoe": "RightToes",
    "lefttoebase": "LeftToes",
    "righttoebase": "RightToes",
}

def normalize_bone_name(name):
    low = name.lower()
    if low in BONE_ALIASES:
        return BONE_ALIASES[low]
    for standard_name in SKELETON_DEF.keys():
        if standard_name.lower() == low:
            return standard_name
    return name


# -----------------------------------------------------------------------------
# VMC UDP 受信スレッド (Thread-safe)
# -----------------------------------------------------------------------------
class VMCReceiverThread(threading.Thread):
    def __init__(self, host="0.0.0.0", port=39539):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.running = False
        self.sock = None
        self.lock = threading.Lock()
        
        # 受信データ
        self.latest_root = None
        self.latest_bones = {}
        
        # 統計
        self.packet_count = 0
        self.last_packet_time = 0.0
        self.fps = 0.0
        self._fps_counter = 0
        self._last_fps_calc_time = time.time()
        self.last_error = ""

    def start_receiver(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.host, self.port))
            self.sock.settimeout(0.5)
            self.running = True
            super().start()
            return True, f"Listening on {self.host}:{self.port}"
        except Exception as e:
            self.last_error = str(e)
            return False, f"Failed to bind socket: {e}"

    def stop_receiver(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def run(self):
        while self.running:
            try:
                data, _ = self.sock.recvfrom(65535)
                if not data:
                    continue
                
                messages = parse_osc_packet(data)
                now = time.time()
                
                with self.lock:
                    self.packet_count += 1
                    self.last_packet_time = now
                    self._fps_counter += 1
                    
                    if now - self._last_fps_calc_time >= 1.0:
                        self.fps = self._fps_counter / (now - self._last_fps_calc_time)
                        self._fps_counter = 0
                        self._last_fps_calc_time = now
                        
                    for addr, args in messages:
                        if addr == '/VMC/Ext/Root/Pos' and len(args) >= 8:
                            name, px, py, pz, qx, qy, qz, qw = args[:8]
                            self.latest_root = {
                                'name': name,
                                'pos': (float(px), float(py), float(pz)),
                                'rot': (float(qx), float(qy), float(qz), float(qw)),
                            }
                        elif addr == '/VMC/Ext/Bone/Pos' and len(args) >= 8:
                            b_name, px, py, pz, qx, qy, qz, qw = args[:8]
                            norm_name = normalize_bone_name(b_name)
                            self.latest_bones[norm_name] = {
                                'raw_name': b_name,
                                'pos': (float(px), float(py), float(pz)),
                                'rot': (float(qx), float(qy), float(qz), float(qw)),
                            }
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.last_error = str(e)
                break

    def get_latest_data(self):
        with self.lock:
            return (
                self.latest_root,
                dict(self.latest_bones),
                self.packet_count,
                self.fps,
                self.last_packet_time,
            )


# -----------------------------------------------------------------------------
# スケルトン生成＆キャラクタライズ ロジック
# -----------------------------------------------------------------------------
class VMCCharacterManager:
    def __init__(self, prefix="VMC"):
        self.prefix = prefix
        self.character = None
        self.bones = {}
        self.root_node = None

    def get_node_name(self, bone_key):
        return f"{self.prefix}_{bone_key}"

    def find_existing_bones(self):
        found = {}
        if not IN_MOTIONBUILDER:
            return found
        for bone_key in SKELETON_DEF.keys():
            name = self.get_node_name(bone_key)
            node = FBFindObjectByFullName(f"Model::{name}")
            if node:
                found[bone_key] = node
        return found

    def create_skeleton(self, log_func=None):
        if not IN_MOTIONBUILDER:
            if log_func: log_func("[TEST MODE] MotionBuilder pyfbsdk is not active.")
            return False, "Not in MotionBuilder environment"

        FBBeginChangeAllModels()
        try:
            self.bones = {}
            # 1. 各ボーンの生成
            for bone_key, info in SKELETON_DEF.items():
                name = self.get_node_name(bone_key)
                existing = FBFindObjectByFullName(f"Model::{name}")
                if existing:
                    node = existing
                else:
                    if info.get("is_null", False):
                        node = FBModelNull(name)
                    else:
                        node = FBModelSkeleton(name)
                        node.Size = 40.0
                        node.Color = FBColor(0.2, 0.75, 0.95)
                    node.Show = True
                self.bones[bone_key] = node

            # 2. 親子関係とTポーズ初期位置・姿勢の設定
            for bone_key, info in SKELETON_DEF.items():
                node = self.bones[bone_key]
                parent_key = info.get("parent")
                if parent_key and parent_key in self.bones:
                    parent_node = self.bones[parent_key]
                    node.Parent = parent_node
                
                lx, ly, lz = info["pos"]
                node.Translation = FBVector3d(lx, ly, lz)
                node.Rotation = FBVector3d(0.0, 0.0, 0.0)

            self.root_node = self.bones.get("Root")
            FBSystem().Scene.Evaluate()

            if log_func:
                log_func(f"Skeleton hierarchy created: {len(self.bones)} nodes.")
            return True, f"Created {len(self.bones)} skeleton nodes"
        except Exception as e:
            if log_func: log_func(f"Error creating skeleton: {e}")
            return False, str(e)
        finally:
            FBEndChangeAllModels()

    def characterize_skeleton(self, log_func=None):
        if not IN_MOTIONBUILDER:
            return False, "Not in MotionBuilder environment"

        char_name = f"{self.prefix}_Character"
        self.character = None
        for char in FBSystem().Scene.Characters:
            if char.Name == char_name:
                self.character = char
                break

        if not self.character:
            self.character = FBCharacter(char_name)

        if self.character.GetCharacterize():
            self.character.SetCharacterizeOff()

        mapped_count = 0
        for bone_key, info in SKELETON_DEF.items():
            slot_name = info.get("slot")
            if not slot_name or bone_key not in self.bones:
                continue
            
            bone_node = self.bones[bone_key]
            slot_prop = self.character.PropertyList.Find(slot_name)
            if slot_prop is not None:
                # 既存の接続があればクリアして追加
                while len(slot_prop) > 0:
                    slot_prop.remove(slot_prop[0])
                slot_prop.append(bone_node)
                mapped_count += 1
            else:
                if log_func: log_func(f"Warning: Slot {slot_name} not found on character.")

        success = self.character.SetCharacterizeOn(True)
        FBSystem().Scene.Evaluate()

        if success:
            msg = f"Character '{char_name}' characterized successfully! (Bones mapped: {mapped_count})"
            if log_func: log_func(msg)
            return True, msg
        else:
            err_msg = self.character.GetCharacterizeError()
            msg = f"Characterize failed: {err_msg}"
            if log_func: log_func(msg)
            return False, msg

    def reset_t_pose(self, log_func=None):
        if not self.bones:
            self.bones = self.find_existing_bones()
        
        for bone_key, info in SKELETON_DEF.items():
            if bone_key in self.bones:
                node = self.bones[bone_key]
                lx, ly, lz = info["pos"]
                node.Translation = FBVector3d(lx, ly, lz)
                node.Rotation = FBVector3d(0.0, 0.0, 0.0)
        
        if IN_MOTIONBUILDER:
            FBSystem().Scene.Evaluate()
        if log_func: log_func("Reset skeleton to standard T-Pose.")

    def delete_skeleton(self, log_func=None):
        if not IN_MOTIONBUILDER:
            return
        
        FBBeginChangeAllModels()
        try:
            char_name = f"{self.prefix}_Character"
            for char in list(FBSystem().Scene.Characters):
                if char.Name == char_name:
                    char.FBDelete()

            existing = self.find_existing_bones()
            for key, node in existing.items():
                node.FBDelete()
            
            self.bones.clear()
            self.root_node = None
            self.character = None
            FBSystem().Scene.Evaluate()
            if log_func: log_func(f"Deleted {len(existing)} skeleton nodes and character.")
        finally:
            FBEndChangeAllModels()


# -----------------------------------------------------------------------------
# メインUIウィジェット (PySide6 / PySide2 QWidget)
# -----------------------------------------------------------------------------
class VMCReceiverWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("VMC Receiver for MotionBuilder 2025")
        self.setMinimumSize(400, 540)

        self.char_mgr = VMCCharacterManager(prefix="VMC")
        self.receiver = None

        self.update_timer = QtCore.QTimer(self)
        self.update_timer.setInterval(16) # ~60 FPS
        self.update_timer.timeout.connect(self.on_update_frame)

        self.setup_style()
        self.setup_ui()
        self.refresh_skeleton_status()

    def setup_style(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #2b2b2b;
                color: #e0e0e0;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QGroupBox {
                border: 1px solid #444;
                border-radius: 6px;
                margin-top: 1.2em;
                padding-top: 10px;
                font-weight: bold;
                color: #58a6ff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QLabel {
                color: #d0d0d0;
                font-size: 12px;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox {
                background-color: #1e1e1e;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 4px 8px;
                color: #ffffff;
                font-size: 12px;
            }
            QLineEdit:focus, QSpinBox:focus {
                border: 1px solid #58a6ff;
            }
            QPushButton {
                background-color: #383838;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 6px 14px;
                color: #f0f0f0;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #484848;
                border-color: #777;
            }
            QPushButton:pressed {
                background-color: #282828;
            }
            QPushButton#btn_primary {
                background-color: #1f6feb;
                border: 1px solid #388bfd;
                color: #ffffff;
            }
            QPushButton#btn_primary:hover {
                background-color: #388bfd;
            }
            QPushButton#btn_connect {
                background-color: #238636;
                border: 1px solid #2ea043;
                color: #ffffff;
            }
            QPushButton#btn_connect:hover {
                background-color: #2ea043;
            }
            QPushButton#btn_disconnect {
                background-color: #da3633;
                border: 1px solid #f85149;
                color: #ffffff;
            }
            QTextEdit {
                background-color: #181818;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                color: #98c379;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
            }
            QCheckBox {
                color: #d0d0d0;
                font-size: 12px;
            }
        """)

    def setup_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # 1. 接続グループ (Connection)
        conn_group = QtWidgets.QGroupBox("1. VMC 接続設定 (Connection)")
        conn_layout = QtWidgets.QGridLayout(conn_group)
        conn_layout.setSpacing(8)

        conn_layout.addWidget(QtWidgets.QLabel("IP アドレス:"), 0, 0)
        self.ip_edit = QtWidgets.QLineEdit("0.0.0.0")
        conn_layout.addWidget(self.ip_edit, 0, 1)

        conn_layout.addWidget(QtWidgets.QLabel("ポート番号:"), 1, 0)
        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(39539)
        conn_layout.addWidget(self.port_spin, 1, 1)

        self.btn_toggle_connect = QtWidgets.QPushButton("受信開始 (Start Receiver)")
        self.btn_toggle_connect.setObjectName("btn_connect")
        self.btn_toggle_connect.clicked.connect(self.toggle_receiver)
        conn_layout.addWidget(self.btn_toggle_connect, 2, 0, 1, 2)

        status_box = QtWidgets.QHBoxLayout()
        self.status_indicator = QtWidgets.QLabel("● 切断中")
        self.status_indicator.setStyleSheet("color: #8b949e; font-weight: bold;")
        self.fps_label = QtWidgets.QLabel("FPS: 0.0 | パケット: 0")
        self.fps_label.setStyleSheet("color: #8b949e;")
        status_box.addWidget(self.status_indicator)
        status_box.addStretch()
        status_box.addWidget(self.fps_label)
        conn_layout.addLayout(status_box, 3, 0, 1, 2)

        main_layout.addWidget(conn_group)

        # 2. スケルトン＆キャラクタライズグループ (Skeleton & Characterize)
        skel_group = QtWidgets.QGroupBox("2. スケルトン＆自動キャラクタライズ")
        skel_layout = QtWidgets.QVBoxLayout(skel_group)
        skel_layout.setSpacing(8)

        prefix_box = QtWidgets.QHBoxLayout()
        prefix_box.addWidget(QtWidgets.QLabel("ボーン接頭辞 (Prefix):"))
        self.prefix_edit = QtWidgets.QLineEdit("VMC")
        self.prefix_edit.textChanged.connect(self.on_prefix_changed)
        prefix_box.addWidget(self.prefix_edit)
        skel_layout.addLayout(prefix_box)

        self.btn_create_char = QtWidgets.QPushButton("スケルトン生成＆キャラクタライズ実行")
        self.btn_create_char.setObjectName("btn_primary")
        self.btn_create_char.setMinimumHeight(34)
        self.btn_create_char.clicked.connect(self.on_create_and_characterize)
        skel_layout.addWidget(self.btn_create_char)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_reset_tpose = QtWidgets.QPushButton("Tポーズにリセット")
        self.btn_reset_tpose.clicked.connect(self.on_reset_tpose)
        btn_row.addWidget(self.btn_reset_tpose)

        self.btn_delete_skel = QtWidgets.QPushButton("スケルトン削除")
        self.btn_delete_skel.clicked.connect(self.on_delete_skeleton)
        btn_row.addWidget(self.btn_delete_skel)
        skel_layout.addLayout(btn_row)

        self.skel_status_label = QtWidgets.QLabel("スケルトン状態: 未作成")
        self.skel_status_label.setStyleSheet("color: #d29922;")
        skel_layout.addWidget(self.skel_status_label)

        main_layout.addWidget(skel_group)

        # 3. 変換オプション (Options)
        opt_group = QtWidgets.QGroupBox("3. 座標系変換オプション")
        opt_layout = QtWidgets.QVBoxLayout(opt_group)
        opt_layout.setSpacing(6)

        self.chk_invert_z = QtWidgets.QCheckBox("Z軸反転 (Unity左手系 → MotionBuilder右手系)")
        self.chk_invert_z.setChecked(True)
        opt_layout.addWidget(self.chk_invert_z)

        scale_box = QtWidgets.QHBoxLayout()
        scale_box.addWidget(QtWidgets.QLabel("位置スケール倍率 (m → cm):"))
        self.scale_spin = QtWidgets.QDoubleSpinBox()
        self.scale_spin.setRange(0.01, 1000.0)
        self.scale_spin.setValue(100.0)
        self.scale_spin.setSingleStep(10.0)
        scale_box.addWidget(self.scale_spin)
        opt_layout.addLayout(scale_box)

        self.chk_apply_hips_trans = QtWidgets.QCheckBox("Hips 位置移動を適用")
        self.chk_apply_hips_trans.setChecked(True)
        opt_layout.addWidget(self.chk_apply_hips_trans)

        main_layout.addWidget(opt_group)

        # 4. ログ出力エリア
        log_group = QtWidgets.QGroupBox("ログ＆検出情報")
        log_layout = QtWidgets.QVBoxLayout(log_group)
        self.log_text = QtWidgets.QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(110)
        log_layout.addWidget(self.log_text)

        main_layout.addWidget(log_group)

        self.log("VMC Receiver Plugin initialized.")
        if IN_MOTIONBUILDER:
            self.log("MotionBuilder pyfbsdk detected. Ready.")
        else:
            self.log("Standalone mode (pyfbsdk not present).")

    def log(self, text):
        t_str = time.strftime("%H:%M:%S")
        self.log_text.append(f"[{t_str}] {text}")
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def on_prefix_changed(self, text):
        self.char_mgr.prefix = text.strip() or "VMC"
        self.refresh_skeleton_status()

    def refresh_skeleton_status(self):
        if not IN_MOTIONBUILDER:
            self.skel_status_label.setText("スケルトン状態: スタンドアロン動作中")
            return
        
        existing = self.char_mgr.find_existing_bones()
        if existing:
            self.char_mgr.bones = existing
            char_name = f"{self.char_mgr.prefix}_Character"
            char_exists = any(c.Name == char_name for c in FBSystem().Scene.Characters)
            if char_exists:
                self.skel_status_label.setText(f"スケルトン状態: 検出済 ({len(existing)}本) / キャラクタライズ済")
                self.skel_status_label.setStyleSheet("color: #3fb950; font-weight: bold;")
            else:
                self.skel_status_label.setText(f"スケルトン状態: 検出済 ({len(existing)}本) / 未キャラクタライズ")
                self.skel_status_label.setStyleSheet("color: #d29922;")
        else:
            self.skel_status_label.setText("スケルトン状態: シーン内に未作成")
            self.skel_status_label.setStyleSheet("color: #8b949e;")

    def on_create_and_characterize(self):
        self.log("Creating skeleton...")
        ok, msg = self.char_mgr.create_skeleton(log_func=self.log)
        if not ok:
            return
        
        self.log("Characterizing skeleton...")
        ok, msg = self.char_mgr.characterize_skeleton(log_func=self.log)
        self.refresh_skeleton_status()

    def on_reset_tpose(self):
        self.char_mgr.reset_t_pose(log_func=self.log)

    def on_delete_skeleton(self):
        reply = QtWidgets.QMessageBox.question(
            self,
            "スケルトン削除確認",
            f"プレフィックス '{self.char_mgr.prefix}' のスケルトンおよびキャラクターを削除しますか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply == QtWidgets.QMessageBox.Yes:
            self.char_mgr.delete_skeleton(log_func=self.log)
            self.refresh_skeleton_status()

    def toggle_receiver(self):
        if self.receiver and self.receiver.running:
            self.receiver.stop_receiver()
            self.receiver = None
            self.update_timer.stop()
            self.btn_toggle_connect.setText("受信開始 (Start Receiver)")
            self.btn_toggle_connect.setObjectName("btn_connect")
            self.btn_toggle_connect.setStyleSheet("")
            self.status_indicator.setText("● 切断中")
            self.status_indicator.setStyleSheet("color: #8b949e; font-weight: bold;")
            self.log("VMC receiver stopped.")
        else:
            ip = self.ip_edit.text().strip() or "0.0.0.0"
            port = self.port_spin.value()
            self.receiver = VMCReceiverThread(host=ip, port=port)
            ok, msg = self.receiver.start_receiver()
            if ok:
                self.btn_toggle_connect.setText("受信停止 (Stop Receiver)")
                self.btn_toggle_connect.setObjectName("btn_disconnect")
                self.btn_toggle_connect.setStyleSheet("")
                self.status_indicator.setText("● 受信待機中")
                self.status_indicator.setStyleSheet("color: #58a6ff; font-weight: bold;")
                self.update_timer.start()
                self.log(msg)
            else:
                self.log(f"Error: {msg}")
                QtWidgets.QMessageBox.critical(self, "接続エラー", msg)

    def on_update_frame(self):
        if not self.receiver or not self.receiver.running:
            return
        
        root_data, bones_data, packet_count, fps, last_time = self.receiver.get_latest_data()
        
        now = time.time()
        is_receiving = (now - last_time < 1.0) and (packet_count > 0)
        if is_receiving:
            self.status_indicator.setText("● 受信中 (Live)")
            self.status_indicator.setStyleSheet("color: #3fb950; font-weight: bold;")
        else:
            self.status_indicator.setText("● 受信待機中 (Waiting...)")
            self.status_indicator.setStyleSheet("color: #d29922; font-weight: bold;")
            
        self.fps_label.setText(f"FPS: {fps:4.1f} | パケット: {packet_count}")

        if not IN_MOTIONBUILDER:
            return

        if not self.char_mgr.bones:
            self.char_mgr.bones = self.char_mgr.find_existing_bones()

        if not self.char_mgr.bones:
            return

        invert_z = self.chk_invert_z.isChecked()
        scale = self.scale_spin.value()
        apply_hips_trans = self.chk_apply_hips_trans.isChecked()

        rot_out = FBVector3d()
        quat_in = FBVector4d()

        try:
            for b_name, b_data in bones_data.items():
                if b_name not in self.char_mgr.bones:
                    continue
                
                bone_node = self.char_mgr.bones[b_name]
                qx, qy, qz, qw = b_data['rot']
                
                if invert_z:
                    qx = -qx
                    qy = -qy
                
                quat_in[0] = qx
                quat_in[1] = qy
                quat_in[2] = qz
                quat_in[3] = qw
                
                FBQuaternionToRotation(rot_out, quat_in)
                bone_node.Rotation = rot_out

                if b_name == "Hips" and apply_hips_trans:
                    px, py, pz = b_data['pos']
                    mx = px * scale
                    my = py * scale
                    mz = -pz * scale if invert_z else pz * scale
                    bone_node.Translation = FBVector3d(mx, my, mz)

            FBSystem().Scene.Evaluate()
        except Exception as e:
            self.log(f"Motion update error: {e}")

    def closeEvent(self, event):
        self.cleanup()
        event.accept()

    def cleanup(self):
        if self.receiver and self.receiver.running:
            self.receiver.stop_receiver()
            self.receiver = None
        self.update_timer.stop()


# -----------------------------------------------------------------------------
# MotionBuilder FBTool (Native Tool Window)
# -----------------------------------------------------------------------------
g_vmc_tool = None
g_vmc_window = None
TOOL_NAME = "VMC Receiver"

def _setup_fbtool():
    """MotionBuilderのネイティブツール(FBTool + FBWidgetHolder)を定義して起動"""
    from pyfbsdk import FBTool, FBWidgetHolder, FBAddRegionParam, FBAttachType, ShowTool
    from pyfbsdk_additions import FBDestroyToolByName, FBAddTool, FBToolList

    class NativeWidgetHolder(FBWidgetHolder):
        def __init__(self):
            super().__init__()
            self.mNativeQtWidget = None

        def WidgetCreate(self, pWidgetParent):
            parentWidget = wrapInstance(pWidgetParent, QtWidgets.QWidget)
            self.mNativeQtWidget = VMCReceiverWidget(parent=parentWidget)
            return getCppPointer(self.mNativeQtWidget)[0]

    class VMCReceiverTool(FBTool):
        def __init__(self, name):
            super().__init__(name)
            self.mNativeWidgetHolder = NativeWidgetHolder()
            self.BuildLayout()
            self.StartSizeX = 460
            self.StartSizeY = 640

        def BuildLayout(self):
            x = FBAddRegionParam(0, FBAttachType.kFBAttachLeft, "")
            y = FBAddRegionParam(0, FBAttachType.kFBAttachTop, "")
            w = FBAddRegionParam(0, FBAttachType.kFBAttachRight, "")
            h = FBAddRegionParam(0, FBAttachType.kFBAttachBottom, "")
            self.AddRegion("main", "main", x, y, w, h)
            self.SetControl("main", self.mNativeWidgetHolder)

    # 既存のツールがあれば一度閉じて破棄
    FBDestroyToolByName(TOOL_NAME)

    tool = VMCReceiverTool(TOOL_NAME)
    FBAddTool(tool)
    ShowTool(tool)
    return tool

def launch():
    """プラグイン起動エントリポイント"""
    global g_vmc_tool, g_vmc_window

    print("=" * 60)
    print("[VMC Receiver] Initializing VMC Receiver Plugin for MotionBuilder...")
    print("=" * 60)

    # 1. MotionBuilder環境でのネイティブFBTool起動を試みる
    if IN_MOTIONBUILDER and wrapInstance is not None and getCppPointer is not None:
        try:
            g_vmc_tool = _setup_fbtool()
            print(f"[VMC Receiver] Native MotionBuilder tool '{TOOL_NAME}' opened successfully!")
            return
        except Exception as e:
            print(f"[VMC Receiver] FBTool creation failed ({e}), falling back to floating Qt window...")

    # 2. フォールバック: スタンドアロンQtウィンドウとして表示
    app = QtWidgets.QApplication.instance()
    is_new = False
    if not app:
        app = QtWidgets.QApplication(sys.argv)
        is_new = True

    if g_vmc_window is not None:
        try:
            g_vmc_window.cleanup()
            g_vmc_window.close()
            g_vmc_window.deleteLater()
        except Exception:
            pass

    g_vmc_window = VMCReceiverWidget()
    g_vmc_window.setWindowFlags(g_vmc_window.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
    g_vmc_window.show()
    g_vmc_window.raise_()
    g_vmc_window.activateWindow()
    print("[VMC Receiver] Floating Qt Window opened successfully!")

    if is_new and not IN_MOTIONBUILDER:
        sys.exit(app.exec())

# ドラッグ＆ドロップまたはスクリプト実行時に即時起動
launch()
