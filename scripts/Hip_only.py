from pythonosc import dispatcher, osc_server
import threading
import pyfbsdk
import math

def quat_to_euler(x, y, z, w):
    """Quaternion → Euler(deg)"""
    # roll (X軸回転)
    sinr_cosp = 2 * (w * x + y * z)
    cosr_cosp = 1 - 2 * (x * x + y * y)
    roll = math.degrees(math.atan2(sinr_cosp, cosr_cosp))

    # pitch (Y軸回転)
    sinp = 2 * (w * y - z * x)
    if abs(sinp) >= 1:
        pitch = math.degrees(math.copysign(math.pi / 2, sinp))  # 90°
    else:
        pitch = math.degrees(math.asin(sinp))

    # yaw (Z軸回転)
    siny_cosp = 2 * (w * z + x * y)
    cosy_cosp = 1 - 2 * (y * y + z * z)
    yaw = math.degrees(math.atan2(siny_cosp, cosy_cosp))

    return roll, pitch, yaw

def handle_bone_pos(address, *args):
    try:
        bone_name = args[0]
        if bone_name != "Hips":   # ✅ Rootだけテスト
            return

        x, y, z = args[1:4]
        rot_x, rot_y, rot_z, rot_w = args[4:8]

        mobu_bone = pyfbsdk.FBFindModelByLabelName("Hips")
        if mobu_bone:
            # === Translation ===
            pos = pyfbsdk.FBVector3d(
                x * 100,
                z * 100,   # Y/Z入れ替え
                y * 100
            )
            mobu_bone.SetVector(pos, pyfbsdk.FBModelTransformationType.kModelTranslation)

            # === Rotation ===
            euler = quat_to_euler(rot_x, rot_y, rot_z, rot_w)
            mobu_bone.SetVector(
                pyfbsdk.FBVector3d(euler[0], euler[1], euler[2]),
                pyfbsdk.FBModelTransformationType.kModelRotation
            )

            print(f"[DEBUG] Hips更新: Pos={pos}, Rot={euler}")

    except Exception as e:
        print(f"[エラー] Root処理失敗: {e}")

def start_vmc_osc_server():
    disp = dispatcher.Dispatcher()
    disp.map("/VMC/Ext/Bone/Pos", handle_bone_pos)

    ip = "127.0.0.1"
    port = 39539
    server = osc_server.ThreadingOSCUDPServer((ip, port), disp)

    print(f"[VMC OSC] 受信開始: {ip}:{port}")
    server.serve_forever()

threading.Thread(target=start_vmc_osc_server, daemon=True).start()
