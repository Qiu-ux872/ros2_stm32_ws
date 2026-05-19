import os
import json
import math
import re
import time
import threading

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseStamped, Twist
from std_msgs.msg import UInt8, String

try:
    import serial
except ImportError:
    serial = None

try:
    import yaml
except ImportError:
    yaml = None

try:
    from ament_index_python.packages import get_package_share_directory
    _PKG_SHARE = get_package_share_directory('asr_bridge')
except Exception:
    _PKG_SHARE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


class AsrBridgeNode(Node):
    def __init__(self):
        super().__init__('asr_bridge_node')

        self._declare_params()
        self._load_command_map()
        self._load_task_points()
        self._create_publishers()
        self._init_serial()

        self.get_logger().info('AsrBridgeNode 已启动')

    def _declare_params(self):
        self.declare_parameter('serial_port', '/dev/ttyUSB2')
        self.declare_parameter('baud_rate', 9600)
        self.declare_parameter('task_points_file', '')
        self.declare_parameter('command_map_file', '')
        self.declare_parameter('serial_timeout', 0.1)
        self.declare_parameter('serial_read_interval', 0.05)

    def _load_command_map(self):
        default_map = {
            'NAV:0':  {'type': 'navigate', 'point_id': 0},
            'NAV:1':  {'type': 'navigate', 'point_id': 1},
            'NAV:2':  {'type': 'navigate', 'point_id': 2},
            'NAV:3':  {'type': 'navigate', 'point_id': 3},
            'NAV:4':  {'type': 'navigate', 'point_id': 4},
            'NAV:5':  {'type': 'navigate', 'point_id': 5},
            'STOP':    {'type': 'stop'},
            'MODE:ROS':     {'type': 'mode', 'value': 1},
            'MODE:MANUAL':  {'type': 'mode', 'value': 0},
            'WAKE':    {'type': 'wake'},
        }

        cmd_file = self.get_parameter('command_map_file').value
        if cmd_file and os.path.exists(cmd_file):
            try:
                with open(cmd_file, 'r', encoding='utf-8') as f:
                    loaded = yaml.safe_load(f) if yaml else json.load(f)
                    if loaded:
                        default_map.update(loaded)
            except Exception as e:
                self.get_logger().warn(f'加载命令映射文件失败: {e}')

        self.command_map = default_map
        self.get_logger().info(f'已加载 {len(self.command_map)} 条命令映射')

    def _load_task_points(self):
        pts_file = self.get_parameter('task_points_file').value
        if not pts_file:
            pts_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                '..', '..', '..',
                'voice_module', 'voice_module', 'point_json',
                'task_points.json'
            )
            pts_file = os.path.normpath(pts_file)

        self.task_points = {}
        if os.path.exists(pts_file):
            try:
                with open(pts_file, 'r', encoding='utf-8') as f:
                    self.task_points = json.load(f)
                self.get_logger().info(
                    f'已加载 {len(self.task_points)} 个任务点: {pts_file}')
            except Exception as e:
                self.get_logger().warn(f'加载任务点文件失败: {e}')
        else:
            self.get_logger().warn(f'任务点文件不存在: {pts_file}')

    def _create_publishers(self):
        self.goal_pub = self.create_publisher(
            PoseStamped, '/voice_nav_goal', 10)
        self.cmd_vel_pub = self.create_publisher(
            Twist, '/cmd_vel', 10)
        self.mode_pub = self.create_publisher(
            UInt8, '/stm32/control_mode', 10)
        self.asr_status_pub = self.create_publisher(
            String, '/asr/status', 10)
        self.asr_raw_pub = self.create_publisher(
            String, '/asr/raw', 10)

    def _init_serial(self):
        self.ser = None
        self.ser_lock = threading.Lock()

        if serial is None:
            self.get_logger().error('pyserial 未安装，串口功能不可用')
            return

        port = self.get_parameter('serial_port').value
        baud = self.get_parameter('baud_rate').value
        timeout = self.get_parameter('serial_timeout').value

        try:
            self.ser = serial.Serial(port, baud, timeout=timeout)
            self.get_logger().info(f'串口已打开: {port} @ {baud}bps')
            self._send_serial('BOOT:OK')
        except Exception as e:
            self.get_logger().warn(f'串口打开失败 ({port}): {e}')

        read_interval = self.get_parameter('serial_read_interval').value
        self.read_timer = self.create_timer(read_interval, self._read_serial)

    def _read_serial(self):
        if self.ser is None or not self.ser.is_open:
            return

        try:
            with self.ser_lock:
                while self.ser.in_waiting > 0:
                    line = self.ser.readline()
                    self._process_line(line)
        except Exception as e:
            self.get_logger().error(f'串口读取错误: {e}')

    def _process_line(self, raw):
        try:
            line = raw.decode('utf-8', errors='ignore').strip()
        except Exception:
            line = raw.decode('latin-1', errors='ignore').strip()

        if not line:
            return

        self.get_logger().debug(f'ASR-PRO 原始数据: {line}')
        self.asr_raw_pub.publish(String(data=line))

        cmd = self._parse_command(line)
        if cmd is None:
            self.get_logger().debug(f'未匹配命令: {line}')
            return

        cmd_type = cmd['type']
        self.get_logger().info(f'识别命令: {line} -> {cmd_type}')
        self.asr_status_pub.publish(String(data=cmd_type))

        if cmd_type == 'navigate':
            self._handle_navigate(cmd['point_id'])
        elif cmd_type == 'stop':
            self._handle_stop()
        elif cmd_type == 'mode':
            self._handle_mode(cmd['value'])
        elif cmd_type == 'wake':
            self._handle_wake()

    def _parse_command(self, line):
        upper = line.upper().strip()

        if upper in self.command_map:
            return self.command_map[upper]

        m = re.match(r'^NAV:(\d+)$', upper)
        if m:
            return {'type': 'navigate', 'point_id': int(m.group(1))}

        m = re.match(r'^MODE:(.+)$', upper)
        if m:
            val = m.group(1).strip()
            if val in ('ROS', 'AUTO', 'AUTOMATIC'):
                return {'type': 'mode', 'value': 1}
            elif val in ('MANUAL', 'MAN'):
                return {'type': 'mode', 'value': 0}

        return None

    def _handle_navigate(self, point_id):
        key = str(point_id)
        if key not in self.task_points:
            self.get_logger().warn(f'任务点 {point_id} 不存在')
            self._send_serial(f'ERR:NOPOINT:{point_id}')
            return

        pt = self.task_points[key]
        goal = PoseStamped()
        goal.header.stamp = self.get_clock().now().to_msg()
        goal.header.frame_id = 'map'
        goal.pose.position.x = float(pt['pose']['x'])
        goal.pose.position.y = float(pt['pose']['y'])
        q = self._euler_to_quaternion(0.0, 0.0, float(pt['pose']['yaw']))
        goal.pose.orientation.x = q[0]
        goal.pose.orientation.y = q[1]
        goal.pose.orientation.z = q[2]
        goal.pose.orientation.w = q[3]

        self.goal_pub.publish(goal)
        env = pt.get('environment', f'点{point_id}')
        self.get_logger().info(
            f'导航目标: 点{point_id} ({env}) '
            f'({goal.pose.position.x:.2f}, {goal.pose.position.y:.2f})')
        self._send_serial(f'OK:NAV:{point_id}')

    def _handle_stop(self):
        stop_twist = Twist()
        self.cmd_vel_pub.publish(stop_twist)
        self.get_logger().info('已发送停止指令')
        self._send_serial('OK:STOP')

    def _handle_mode(self, value):
        msg = UInt8(data=value)
        self.mode_pub.publish(msg)
        mode_name = 'ROS控制' if value == 1 else '手动模式'
        self.get_logger().info(f'已切换模式: {mode_name}')
        self._send_serial(f'OK:MODE:{value}')

    def _handle_wake(self):
        self.get_logger().info('ASR-PRO 唤醒')
        self._send_serial('OK:WAKE')

    def _send_serial(self, text):
        if self.ser is None or not self.ser.is_open:
            return
        try:
            with self.ser_lock:
                self.ser.write((text + '\n').encode('utf-8'))
                self.ser.flush()
        except Exception as e:
            self.get_logger().error(f'串口发送失败: {e}')

    @staticmethod
    def _euler_to_quaternion(roll, pitch, yaw):
        cx = math.cos(roll * 0.5)
        sx = math.sin(roll * 0.5)
        cy = math.cos(pitch * 0.5)
        sy = math.sin(pitch * 0.5)
        cz = math.cos(yaw * 0.5)
        sz = math.sin(yaw * 0.5)
        return [
            sx * cy * cz - cx * sy * sz,
            cx * sy * cz + sx * cy * sz,
            cx * cy * sz - sx * sy * cz,
            cx * cy * cz + sx * sy * sz,
        ]

    def destroy_node(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = AsrBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
