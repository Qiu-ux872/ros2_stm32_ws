import json
import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from nav2_msgs.action import NavigateToPose
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
import paho.mqtt.client as mqtt
import math


class VoiceBridgeNode(Node):
    def __init__(self):
        super().__init__('voice_bridge_node')

        # ===== MQTT 配置（用于非ROS2设备通信） =====
        self.mqtt_host = self.declare_parameter('mqtt_host', '10.42.0.1').value
        self.mqtt_port = self.declare_parameter('mqtt_port', 1883).value
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_msg
        self.mqtt_client.connect_async(self.mqtt_host, self.mqtt_port)
        self.mqtt_client.loop_start()

        # ===== ROS2 订阅：从语音模块接收导航目标（主要来源） =====
        self.create_subscription(
            PoseStamped, '/voice_nav_goal', self.voice_goal_cb, 10)

        # ===== Nav2 Action 客户端 =====
        self.nav_client = ActionClient(self, NavigateToPose, 'navigate_to_pose')

        # ===== 订阅 AMCL 定位结果 =====
        self.create_subscription(
            PoseWithCovarianceStamped, '/amcl_pose', self.amcl_cb, 10)

        # ===== 超时保护：5s无指令自动停车 =====
        self.last_goal_time = self.get_clock().now()
        self.create_timer(1.0, self.check_timeout)

        self.get_logger().info('VoiceBridgeNode 已启动（监听 /voice_nav_goal）')

    # ---------- ROS2 回调：来自语音模块 ----------
    def voice_goal_cb(self, goal):
        self.last_goal_time = self.get_clock().now()
        self.get_logger().info(
            f'收到语音导航目标: ({goal.pose.position.x:.2f}, {goal.pose.position.y:.2f})')
        self.send_nav_goal(goal)

    # ---------- MQTT 回调（备选通道） ----------
    def on_mqtt_connect(self, client, userdata, flags, rc):
        self.get_logger().info(f'MQTT 已连接(rc={rc})')
        client.subscribe('robot_control', qos=2)

    def on_mqtt_msg(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
            if data.get('cmd_type') == 'goal_point_set':
                self.last_goal_time = self.get_clock().now()
                goal = PoseStamped()
                goal.header.stamp = self.get_clock().now().to_msg()
                goal.header.frame_id = 'map'
                goal.pose.position.x = float(data['x'])
                goal.pose.position.y = float(data['y'])
                q = self.euler_to_quaternion(0, 0, float(data['z']))
                goal.pose.orientation.x = q[0]
                goal.pose.orientation.y = q[1]
                goal.pose.orientation.z = q[2]
                goal.pose.orientation.w = q[3]
                self.send_nav_goal(goal)
        except Exception as e:
            self.get_logger().error(f'MQTT 消息解析失败:{e}')

    # ---------- 发送导航目标到 Nav2 ----------
    def send_nav_goal(self, goal):
        if not self.nav_client.wait_for_server(timeout_sec=1.0):
            self.get_logger().warn('Nav2 action 服务未就绪')
            return
        goal_msg = NavigateToPose.Goal()
        goal_msg.pose = goal
        self.get_logger().info(
            f'发送导航目标:({goal.pose.position.x:.2f}, {goal.pose.position.y:.2f})')
        self.nav_client.send_goal_async(goal_msg)

    # ---------- AMCL 位姿回调 ----------
    def amcl_cb(self, msg):
        pose = msg.pose.pose
        roll, pitch, yaw = self.quaternion_to_euler(
            pose.orientation.x, pose.orientation.y,
            pose.orientation.z, pose.orientation.w
        )
        self.mqtt_client.publish('robot_pose', json.dumps({
            'x': round(pose.position.x, 2),
            'y': round(pose.position.y, 2),
            'yaw': round(yaw, 2)
        }))

    # ---------- 超时检查 ----------
    def check_timeout(self):
        elapsed = (self.get_clock().now() - self.last_goal_time).nanoseconds / 1e9
        if elapsed > 5.0:
            self.get_logger().warn(f'已{elapsed:.0f}s无新指令，准备停车')


def main(args=None):
    rclpy.init(args=args)
    node = VoiceBridgeNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


    @staticmethod
    def euler_to_quaternion(roll, pitch, yaw):
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

    @staticmethod
    def quaternion_to_euler(x, y, z, w):
        t0 = +2.0 * (w * x + y * z)
        t1 = +1.0 - 2.0 * (x * x + y * y)
        roll = math.atan2(t0, t1)
        t2 = +2.0 * (w * y - z * x)
        t2 = max(-1.0, min(1.0, t2))
        pitch = math.asin(t2)
        t3 = +2.0 * (w * z + x * y)
        t4 = +1.0 - 2.0 * (y * y + z * z)
        yaw = math.atan2(t3, t4)
        return [roll, pitch, yaw]


if __name__ == '__main__':
    main()
