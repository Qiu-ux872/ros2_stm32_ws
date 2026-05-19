import os
import time
import json
import wave
import threading
import cv2
from PIL import Image 
import pygame
import pyaudio
import torch
import webrtcvad
import numpy as np
import re
from queue import Queue, Empty
from transformers import AutoProcessor
from qwen_vl_utils import process_vision_info 
from modelscope import AutoModel
from modelscope.pipelines import pipeline
from modelscope.utils.constant import Tasks
from transformers import Qwen2VLForConditionalGeneration
from transformers import BitsAndBytesConfig

import torch
torch.cuda.empty_cache()
torch.backends.cudnn.benchmark = True

from scipy.signal import resample_poly 
from collections import deque

import math
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseStamped

AUDIO_CHANNELS = 1
AUDIO_RATE = 48000 
AUDIO_CHUNK = 1024
VAD_MODE = 3
NO_SPEECH_THRESHOLD = 4
TTS_RATE = 150
TTS_VOLUME = 0.9
POINT_JSON_DIR = "point_json"
os.makedirs(POINT_JSON_DIR, exist_ok=True)
TASK_POINTS_FILE = os.path.join(POINT_JSON_DIR, "task_points.json")


class CommandType:
    A_TO_B = 1
    B_TO_C = 2
    C_TO_D = 3
    D_TO_A = 4
    IMAGE_RECOGNITION = 5
    DIALOGUE = 6
    ADD_TASK_POINT = 7
    MODIFY_TASK_POINT = 8
    NAVIGATE = 9
    GREET = 10


class VoiceNavigationSystem:
    def __init__(self, goal_publisher=None, node=None):
        self.node = node
        self.goal_publisher = goal_publisher

        self.mqtt_broker = '10.42.0.1'
        self.mqtt_port = 1883
        self.mqtt_control_topic = 'robot_control'
        self.mqtt_status_topic = 'base_status'
        self.qwen_model_path = '/home/y/LLM/Qwen2-VL-2B-Instruct'
        self.sensevoice_model_path = '/home/y/LLM/SenseVoiceSmall'
        print(f"任务点文件路径: {TASK_POINTS_FILE}")

        self.device = None
        self._initialize_device()

        self.audio_stream = None
        self.pyaudio_instance = None

        self.mqtt_client = None
        self.initialize_mqtt()
        self.tts_engine = None
        self.tts_lock = threading.Lock()
        self.command_queue = Queue()
        self.is_running = False
        self.is_listening = False
        self.FORMAT = pyaudio.paInt16
        self.tts_lock = threading.Lock()
        self.audio_block_lock = threading.Lock()

        self.audio_queue = Queue()
        self.video_queue = Queue(maxsize=100)
        self.video_buffer = deque(maxlen=300)
        self.last_active_time = time.time()
        self.recording_active = True
        self.segments_to_save = []
        self.saved_intervals = []
        self.last_vad_end_time = 0

        self.vad = webrtcvad.Vad()
        self.vad.set_mode(VAD_MODE)

        self.task_points = self._load_task_points()
        self.current_pose = None
        self.last_nav_command = None
        self.last_nav_query = ""

        self.is_cycling = False
        self.current_cycle_step = 0
        self.cycle_order = [1, 2, 3, 0]
        self.completed_cycles = 0
        self.max_cycles = 1

        pygame.mixer.init()
        self.ARM_STATUS_TO_POINT = {
            0: 1,
            1: 2,
            2: 3,
            3: 0
        }
        self.command_mapping = {
            "开始导航": (CommandType.NAVIGATE, 6),
            "启动导航": (CommandType.NAVIGATE, 6),
            "启动循环": (CommandType.NAVIGATE, 6),
            "开始循环": (CommandType.NAVIGATE, 6),
            "停止循环": (CommandType.NAVIGATE, 7),
            "结束循环": (CommandType.NAVIGATE, 7),

            "去一号工作点": (CommandType.NAVIGATE, 1),
            "一号工作点": (CommandType.NAVIGATE, 1),
            "去一号点": (CommandType.NAVIGATE, 1),
            "去一号工作店": (CommandType.NAVIGATE, 1),
            "一号工作店": (CommandType.NAVIGATE, 1),
            "去一号店": (CommandType.NAVIGATE, 1),
            "去一号": (CommandType.NAVIGATE, 1),

            "去二号工作点": (CommandType.NAVIGATE, 2),
            "二号工作点": (CommandType.NAVIGATE, 2),
            "去二号点": (CommandType.NAVIGATE, 2),
            "去二号工作店": (CommandType.NAVIGATE, 2),
            "二号工作店": (CommandType.NAVIGATE, 2),
            "去二号店": (CommandType.NAVIGATE, 2),
            "去二号": (CommandType.NAVIGATE, 2),

            "去三号工作点": (CommandType.NAVIGATE, 3),
            "三号工作点": (CommandType.NAVIGATE, 3),
            "去三号点": (CommandType.NAVIGATE, 3),
            "去三号工作店": (CommandType.NAVIGATE, 3),
            "三号工作店": (CommandType.NAVIGATE, 3),
            "去三号店": (CommandType.NAVIGATE, 3),
            "去三号": (CommandType.NAVIGATE, 3),

            "返回": (CommandType.NAVIGATE, 0),
            "回家": (CommandType.NAVIGATE, 0),
            "返程": (CommandType.NAVIGATE, 0),
            "去起始工作点": (CommandType.NAVIGATE, 0),
            "起始工作点": (CommandType.NAVIGATE, 0),
            "去起始点": (CommandType.NAVIGATE, 0),
            "去起始": (CommandType.NAVIGATE, 0),
            "去起点": (CommandType.NAVIGATE, 0),
            "去起始工作店": (CommandType.NAVIGATE, 0),
            "起始工作店": (CommandType.NAVIGATE, 0),
            "去起始店": (CommandType.NAVIGATE, 0),
            "去起店": (CommandType.NAVIGATE, 0),

            "摄像头识别到什么": CommandType.IMAGE_RECOGNITION,
            "识别一下": CommandType.IMAGE_RECOGNITION,
            "这是什么": CommandType.IMAGE_RECOGNITION,

            "添加任务点": CommandType.ADD_TASK_POINT,
            "增加任务点": CommandType.ADD_TASK_POINT,
            "保存当前点": CommandType.ADD_TASK_POINT,
            "记录这个点": CommandType.ADD_TASK_POINT,

            "设定为一号点": (CommandType.MODIFY_TASK_POINT, 1),
            "设置为一号点": (CommandType.MODIFY_TASK_POINT, 1),
            "置为一号点": (CommandType.MODIFY_TASK_POINT, 1),
            "设为一号点": (CommandType.MODIFY_TASK_POINT, 1),
            "改为一号点": (CommandType.MODIFY_TASK_POINT, 1),
            "修改为一号点": (CommandType.MODIFY_TASK_POINT, 1),
            "为一号点": (CommandType.MODIFY_TASK_POINT, 1),

            "设定为二号点": (CommandType.MODIFY_TASK_POINT, 2),
            "设置为二号点": (CommandType.MODIFY_TASK_POINT, 2),
            "设为二号点": (CommandType.MODIFY_TASK_POINT, 2),
            "置为二号点": (CommandType.MODIFY_TASK_POINT, 2),
            "改为二号点": (CommandType.MODIFY_TASK_POINT, 2),
            "修改为二号点": (CommandType.MODIFY_TASK_POINT, 2),
            "为二号点": (CommandType.MODIFY_TASK_POINT, 2),

            "设定为三号点": (CommandType.MODIFY_TASK_POINT, 3),
            "设置为三号点": (CommandType.MODIFY_TASK_POINT, 3),
            "置为三号点": (CommandType.MODIFY_TASK_POINT, 3),
            "设为三号点": (CommandType.MODIFY_TASK_POINT, 3),
            "改为三号点": (CommandType.MODIFY_TASK_POINT, 3),
            "修改为三号点": (CommandType.MODIFY_TASK_POINT, 3),
            "为三号点": (CommandType.MODIFY_TASK_POINT, 3),

            "设定为起始点": (CommandType.MODIFY_TASK_POINT, 0),
            "设置为起始点": (CommandType.MODIFY_TASK_POINT, 0),
            "置为起始点": (CommandType.MODIFY_TASK_POINT, 0),
            "设为起始点": (CommandType.MODIFY_TASK_POINT, 0),
            "改为起始点": (CommandType.MODIFY_TASK_POINT, 0),
            "修改为起始点": (CommandType.MODIFY_TASK_POINT, 0),
            "为起始点": (CommandType.MODIFY_TASK_POINT, 0),

            "打个招呼": (CommandType.GREET, 9),
            "打个": (CommandType.GREET, 9),
            "招呼": (CommandType.GREET, 9),
            "问候一下": (CommandType.GREET, 9),
            "问候": (CommandType.GREET, 9),
            "问好": (CommandType.GREET, 9),
        }

        self.welcome_text = "大家好，我是语音导航助手，请说出您的指令"
        self.listening_text = "我在听，请说话"

        print("智能语音导航系统初始化中...")
        self.initialize_system()

    def _initialize_device(self):
        try:
            if torch.cuda.is_available():
                self.device = torch.device("cuda")
                print(f"CUDA可用，使用GPU: {torch.cuda.get_device_name(0)}")
            else:
                self.device = torch.device("cpu")
                print("CUDA不可用，使用CPU")
            test_tensor = torch.tensor([1.0], device=self.device)
            return True
        except Exception as e:
            print(f"设备初始化失败: {e}")
            self.device = torch.device("cpu")
            print("强制使用CPU作为 fallback")
            return False

    def initialize_models(self):
        try:
            if self.device is None:
                self._initialize_device()

            print("加载千问VL模型...")
            min_pixels = 128 * 28 * 28
            max_pixels = 256 * 28 * 28

            bnb_config = BitsAndBytesConfig(
                load_in_8bit=True,
                bnb_8bit_use_double_quant=True,
                bnb_8bit_compute_dype=torch.float16
            )
            self.qwen_model = Qwen2VLForConditionalGeneration.from_pretrained(
                self.qwen_model_path,
                quantization_config=bnb_config,
                device_map="auto",
                trust_remote_code=True
            )
            self.qwen_model.eval()

            self.qwen_processor = AutoProcessor.from_pretrained(
                self.qwen_model_path,
                min_pixels=min_pixels,
                max_pixels=max_pixels,
                trust_remote_code=True
            )

            print("加载SenseVoice模型...")
            self.sensevoice_pipeline = pipeline(
                task=Tasks.auto_speech_recognition,
                model=self.sensevoice_model_path,
                model_revision="v1.0.0",
                trust_remote_code=True
            )
            print("SenseVoice模型加载成功")

            print(f"所有模型已成功加载到 {self.device}")
            return True
        except Exception as e:
            print(f"模型加载失败: {e}")
            import traceback
            print(f"错误详情: {traceback.format_exc()}")
            return False

    def initialize_mqtt(self):
        try:
            import paho.mqtt.client as mqtt
            self.mqtt_client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
            self.mqtt_client.on_connect = self.on_connect
            self.mqtt_client.on_message = self.on_message
            self.mqtt_client.connect(self.mqtt_broker, self.mqtt_port, keepalive=60)
            self.mqtt_client.loop_start()
            return True
        except Exception as e:
            print(f"MQTT连接失败: {e}")
            return False

    def initialize_tts(self):
        try:
            import pyttsx3
            self.tts_engine = pyttsx3.init(driverName='espeak')
            self.tts_engine.setProperty('rate', TTS_RATE)
            self.tts_engine.setProperty('volume', TTS_VOLUME)

            voices = self.tts_engine.getProperty('voices')
            print("可用语音列表：")
            for i, voice in enumerate(voices):
                print(f"索引 {i}：ID={voice.id}，名称={voice.name}，语言={voice.languages}")

            selected_voice = None
            if len(voices) > 12:
                selected_voice = voices[12].id
            else:
                for voice in voices:
                    for lang in voice.languages:
                        if isinstance(lang, bytes) and lang.decode('utf-8').startswith(('cmn', 'zh')):
                            selected_voice = voice.id
                            break
            if selected_voice:
                self.tts_engine.setProperty('voice', selected_voice)
                print(f"成功设置中文语音：{selected_voice}")
            else:
                print("未找到中文语音，可能导致播报异常")

            return True
        except Exception as e:
            print(f"离线TTS初始化失败: {e}")
            self.tts_engine = None
            return False

    def speak(self, text):
        filtered_text = re.sub(r'[^\u4e00-\u9fa5，。？！,.;?!\s]', '', text).strip()
        if not filtered_text:
            print("过滤后无有效文本，无法播报")
            return
        def _speak_offline():
            if not self.tts_engine:
                print(f"TTS未初始化，无法播报: {text}")
                return
            try:
                with self.tts_lock:
                    with self.audio_block_lock:
                        self.tts_engine.say(filtered_text)
                        self.tts_engine.runAndWait()
            except Exception as e:
                print(f"离线播报错误: {e}")
        threading.Thread(target=_speak_offline, daemon=True).start()

    def _load_task_points(self):
        if os.path.exists(TASK_POINTS_FILE):
            try:
                with open(TASK_POINTS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"加载任务点失败: {e}")
        return {}

    def _save_task_points(self):
        try:
            if not os.path.exists(os.path.dirname(TASK_POINTS_FILE)):
                os.makedirs(os.path.dirname(TASK_POINTS_FILE), exist_ok=True)
                print(f"创建目录: {os.path.dirname(TASK_POINTS_FILE)}")

            with open(TASK_POINTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.task_points, f, ensure_ascii=False, indent=2)

            print(f"任务点已保存至 {TASK_POINTS_FILE}")
            return True
        except PermissionError:
            print(f"保存失败：无权限写入文件 {TASK_POINTS_FILE}")
            return False
        except Exception as e:
            print(f"保存失败：{e}")
            return False

    def _get_scene_description(self, frame):
        try:
            if frame is None:
                return "未获取到画面"

            pil_image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

            prompt = """识别图片中的核心物品（1-2个），遵循：
            1. 只说物体名称（如"打印机"）；
            2. 忽略颜色、位置等修饰；
            3. 若有多个，选最显眼的。
            十字以内完成。
            """
            messages = [
                {"role": "observer", "content": [
                    {"type": "image", "image": pil_image},
                    {"type": "text", "text": prompt}
                ]}
            ]

            text = self.qwen_processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            image_inputs, _ = process_vision_info(messages)
            inputs = self.qwen_processor(
                text=[text], images=image_inputs, padding=True, return_tensors="pt"
            ).to(self.device)

            generated_ids = self.qwen_model.generate(**inputs, max_new_tokens=50)
            generated_ids_trimmed = generated_ids[0][len(inputs['input_ids'][0]):]
            description = self.qwen_processor.decode(generated_ids_trimmed, skip_special_tokens=True).strip()
            print(f"description: {description}")
            return description if description else "场景描述失败"
        except Exception as e:
            print(f"场景描述错误: {e}")
            return "场景识别失败"

    def add_task_point(self):
        if not self.current_pose:
            self.speak("未获取到底盘位置")
            return

        x, y, yaw = self.current_pose

        latest_frame = self._get_latest_frame()
        scene_desc = self._get_scene_description(latest_frame)
        if self.task_points:
            max_existing_id = max(int(point_info["id"]) for point_info in self.task_points.values())
            new_id = max_existing_id + 1
        else:
            new_id = 0

        task_point_info = {
            "id": new_id,
            "pose": {
                "x": round(x, 3),
                "y": round(y, 3),
                "yaw": round(yaw, 3)
            },
            "environment": scene_desc
        }

        self.task_points[str(new_id)] = task_point_info
        self._save_task_points()

        self.speak(
            f"已添加任务点{new_id}，"
            f"位置：x={x:.2f}, y={y:.2f}，"
            f"场景：{scene_desc}"
        )
        print(f"添加任务点{new_id}: {task_point_info}")

    def modify_task_point(self, point_id):
        print(f"收到指令：point_id={point_id}")
        if not self.current_pose:
            self.speak("未获取到底盘位置，无法修改任务点")
            print("modify_task_point: current_pose为空")
            return

        target_key = str(point_id)
        if target_key not in self.task_points:
            self.speak(f"未找到{point_id}号任务点")
            print(f"modify_task_point: 任务点{point_id}不存在")
            return

        print(f"修改前 - 任务点{point_id}: {self.task_points[target_key]}")

        x, y, yaw = self.current_pose
        latest_frame = self._get_latest_frame()
        scene_desc = self._get_scene_description(latest_frame)
        print(f"新数据 - 位姿: (x={x}, y={y}, yaw={yaw}), 场景: {scene_desc}")

        self.task_points[target_key] = {
            "id": int(point_id),
            "pose": {
                "x": round(x, 3),
                "y": round(y, 3),
                "yaw": round(yaw, 3)
            },
            "environment": scene_desc
        }

        print(f"修改后 - 任务点{point_id}: {self.task_points[target_key]}")
        save_success = self._save_task_points()
        if save_success:
            self.speak(f"已将当前位置设为{point_id}号点，场景：{scene_desc}")
        else:
            self.speak(f"修改任务点{point_id}失败，文件保存出错")

    def find_point_by_environment(self, query):
        if not self.task_points:
            return None

        stop_words = {'去', '拿', '取', '找', '到', '在', '往', '向', '的', '了', '是'}
        query_clean = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', ' ', query)
        query_words = re.findall(r'[\u4e00-\u9fa5a-zA-Z0-9]+', query_clean)
        query_words = [word for word in query_words if word not in stop_words]

        if not query_words:
            print(f"过滤后无有效关键词: {query}")
            return None

        print(f"提取的有效关键词: {query_words}")

        best_match = None
        max_score = 0
        match_details = []

        for point_id, point_info in self.task_points.items():
            desc = point_info.get("environment", "").strip()
            if not desc:
                continue

            matched_words = []
            for word in query_words:
                if word in desc or desc in word:
                    matched_words.append(word)

            score = len(matched_words)
            desc_words = len(re.findall(r'[\u4e00-\u9fa5a-zA-Z0-9]+', desc))
            if desc_words > 0:
                score += score / desc_words

            match_details.append(f"任务点{point_id} (描述: {desc}): 匹配关键词 {matched_words}, 得分 {score:.2f}")
            print(f"匹配详情: {match_details[-1]}")

            if score > max_score:
                max_score = score
                best_match = point_id

        if max_score < 0.3:
            print(f"没有找到足够匹配的任务点，最高得分仅为 {max_score:.2f}")
            return None

        print(f"最佳匹配: 任务点{best_match}，得分 {max_score:.2f}")
        return best_match

    # ===== ROS2 改造核心：send_goal_point 使用 ROS2 Publisher 替代 MQTT =====
    def send_goal_point(self, point_id):
        point_key = str(point_id)
        if point_key not in self.task_points:
            self.speak(f"未找到{point_id}号任务点")
            return False

        point_info = self.task_points[point_key]

        if self.goal_publisher and self.node:
            goal = PoseStamped()
            goal.header.stamp = self.node.get_clock().now().to_msg()
            goal.header.frame_id = 'map'
            goal.pose.position.x = point_info["pose"]["x"]
            goal.pose.position.y = point_info["pose"]["y"]
            q = euler_to_quaternion(0, 0, point_info["pose"]["yaw"])
            goal.pose.orientation.x = q[0]
            goal.pose.orientation.y = q[1]
            goal.pose.orientation.z = q[2]
            goal.pose.orientation.w = q[3]
            self.goal_publisher.publish(goal)

            if hasattr(self.node, 'get_logger'):
                self.node.get_logger().info(
                    f'语音模块发送导航目标: 点{point_id} '
                    f'({point_info["pose"]["x"]:.2f}, {point_info["pose"]["y"]:.2f})'
                )
        else:
            message = {
                "cmd_type": "goal_point_set",
                "x": point_info["pose"]["x"],
                "y": point_info["pose"]["y"],
                "z": point_info["pose"]["yaw"]
            }
            try:
                cmd_json = json.dumps(message)
                for _ in range(3):
                    self.mqtt_client.publish(self.mqtt_control_topic, cmd_json, qos=2)
                    time.sleep(0.1)
            except Exception as e:
                print(f"MQTT发送失败: {e}")
                return False

        if self.is_cycling:
            step = self.current_cycle_step + 1
            self.speak(f"已前往{point_id}号点（循环步骤{step}/4），等待机械臂完成工作")
        else:
            self.speak(f"已发送前往{point_id}号点的指令")

        self.send_lerobot(point_id)
        print(f"发送目标点{point_id}: {point_info['pose']}")
        return True

    def send_lerobot(self, number):
        try:
            if not isinstance(number, int) or number < 0:
                raise ValueError(f"无效的数字: {number}，必须是非负整数")

            message = {
                "type": "greeting",
                "number": number
            }
            command_json = json.dumps(message)

            for _ in range(3):
                self.mqtt_client.publish(
                    topic="lerobot_status",
                    payload=command_json,
                    qos=2
                )
                time.sleep(0.1)

            print(f"已向lerobot_status发送数字: {number}")

            if number == 9:
                self.speak("你好，我是小车，需要帮助吗")
        except Exception as e:
            print(f"发送打招呼消息失败: {e}")
            self.speak("打招呼失败，请重试")

    def start_cycle_navigation(self):
        if not self.task_points:
            self.speak("未找到任何任务点，无法启动循环")
            return

        self.completed_cycles = 0
        self.is_cycling = True
        self.current_cycle_step = 0
        first_point = str(self.cycle_order[0])

        if first_point not in self.task_points:
            self.speak(f"循环起始点{first_point}不存在，循环终止")
            self.is_cycling = False
            return

        self.speak(f"启动循环导航，前往{first_point}号点")
        self.send_goal_point(first_point)

    def on_connect(self, client, userdata, flags, rc, properties=None):
        print(f"MQTT连接成功,状态码: {rc}")
        client.subscribe(self.mqtt_status_topic)
        client.subscribe("script/grab_status")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())

            if msg.topic == "script/grab_status" and "grab_successfully" in payload and self.is_cycling:
                arm_status = payload.get('grab_successfully', -1)
                current_point = self.cycle_order[self.current_cycle_step]
                print(f"从 {msg.topic} 收到机械臂状态: {arm_status}（当前点：{current_point}，循环步骤：{self.current_cycle_step}）")

                status_to_target = {
                    1: 2,
                    2: 3,
                    3: 0,
                    0: None,
                    -1: None
                }

                target_point = status_to_target.get(arm_status, None)

                if arm_status == -1:
                    self.speak("未获取到机械臂有效工作状态，循环暂停")
                    self.is_cycling = False
                    return

                elif arm_status == 0:
                    self.completed_cycles += 1
                    self.speak(f"已完成单次循环任务（1→2→3→0），共完成{self.completed_cycles}次")
                    self.is_cycling = False
                    return

                elif target_point is not None:
                    try:
                        next_step = self.cycle_order.index(target_point)
                        self.speak(f"机械臂在{current_point}号点工作完成，即将前往{target_point}号点（循环步骤{next_step+1}/4）")
                        self.current_cycle_step = next_step
                        if str(target_point) in self.task_points:
                            self.send_goal_point(str(target_point))
                        else:
                            self.speak(f"循环中断：未找到{target_point}号任务点")
                            self.is_cycling = False
                    except ValueError:
                        self.speak(f"循环配置错误：目标点{target_point}不在循环顺序中")
                        self.is_cycling = False

                else:
                    self.speak(f"收到未知机械臂状态{arm_status}，循环暂停")
                    self.is_cycling = False
                return

            if msg.topic == self.mqtt_status_topic and "pose" in payload:
                pose = payload["pose"]
                self.current_pose = (pose["x"], pose["y"], pose["yaw"])

        except json.JSONDecodeError:
            print("MQTT消息解析失败")
            self.speak("指令解析错误，请重试")
        except Exception as e:
            print(f"处理MQTT消息错误: {e}")

    def process_image_recognition(self):
        try:
            latest_frame = self._get_latest_frame()
            if latest_frame is None:
                self.speak("无法获取摄像头画面")
                return

            pil_image = Image.fromarray(cv2.cvtColor(latest_frame, cv2.COLOR_BGR2RGB))

            prompt = "请详细描述画面中的内容，包含所有可见的物体、人物和环境信息"
            messages = [
                {"role": "user", "content": [
                    {"type": "image", "image": pil_image},
                    {"type": "text", "text": prompt}
                ]}
            ]

            text = self.qwen_processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            image_inputs, _ = process_vision_info(messages)
            inputs = self.qwen_processor(
                text=[text], images=image_inputs, padding=True, return_tensors="pt"
            ).to(self.device)

            generated_ids = self.qwen_model.generate(**inputs, max_new_tokens=200)
            generated_ids_trimmed = generated_ids[0][len(inputs['input_ids'][0]):]
            result = self.qwen_processor.decode(generated_ids_trimmed, skip_special_tokens=True).strip()

            print(f"图像识别结果: {result}")
            self.speak(f"我看到{result}")
        except Exception as e:
            print(f"图像识别错误: {e}")
            self.speak("图像识别失败")

    def extract_numbers_from_result(self, recognition_result: str) -> list:
        numbers = re.findall(r'\d+', recognition_result)
        return [int(num) for num in numbers]

    def match_task_point_by_number(self, recognition_result: str):
        numbers = self.extract_numbers_from_result(recognition_result)
        for num in numbers:
            if str(num) in self.task_points:
                return str(num)
        return None

    def process_dialogue(self, text):
        try:
            latest_frame = self._get_latest_frame()
            frames = [latest_frame] if latest_frame is not None else []

            if frames:
                pil_images = [Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)) for frame in frames]
                messages = [
                    {"role": "user", "content": [
                        {"type": "image", "image": pil_images[0]},
                        {"type": "text", "text": text}
                    ]}
                ]
            else:
                messages = [
                    {"role": "user", "content": [
                        {"type": "text", "text": text}
                    ]}
                ]

            prompt_text = self.qwen_processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            image_inputs, _ = process_vision_info(messages) if frames else (None, None)

            inputs = self.qwen_processor(
                text=[prompt_text], images=image_inputs,
                padding=True, return_tensors="pt"
            ).to(self.device)

            generated_ids = self.qwen_model.generate(**inputs, max_new_tokens=200)
            generated_ids_trimmed = generated_ids[0][len(inputs['input_ids'][0]):]
            response = self.qwen_processor.decode(generated_ids_trimmed, skip_special_tokens=True).strip()

            print(f"对话回复: {response}")
            self.speak(response[:80])
        except Exception as e:
            print(f"对话处理错误: {e}")
            self.speak("对话处理失败")

    def check_dialogue_for_environment(self, user_query, model_response):
        combined_text = f"{user_query} {model_response}"
        nav_keywords = ["去", "到", "拿", "取", "找", "水", "书", "桌子", "椅子", "瓶子", "遥控器"]
        matched_keywords = [kw for kw in nav_keywords if kw in combined_text]
        if matched_keywords:
            print(f"对话中包含环境导航关键词: {matched_keywords}，尝试匹配任务点")
            matched_point = self.find_point_by_environment(combined_text)
            if matched_point:
                self.speak(f"根据对话内容找到匹配位置，正在导航")
                self.send_goal_point(matched_point)
                return True
        return False

    def handle_navigation(self, command_code=None, query_text=""):
        if command_code is not None and int(command_code) == 6:
            self.start_cycle_navigation()
            return True
        if command_code is not None and int(command_code) == 7:
            self.is_cycling = False
            self.speak("已停止循环任务")
            return True

        matched_point = None
        if query_text:
            print(f"尝试按描述导航: {query_text}")
            matched_point = self.find_point_by_environment(query_text)

        if matched_point:
            self.speak(f"找到匹配的位置，正在导航过去")
            success = self.send_goal_point(matched_point)
            if success:
                return True

        if command_code is not None:
            print(f"按任务点ID导航: {command_code}")
            target_key = str(command_code)
            if target_key in self.task_points:
                self.send_goal_point(target_key)
                return True
            else:
                self.speak(f"未找到{command_code}号任务点，请先添加")
                return False

        self.speak("没有找到匹配的导航目标，请提供更明确的指令")
        return False

    def _get_camera_capture(self):
        import glob
        import re
        camera_devices = glob.glob("/dev/video*")
        if not camera_devices:
            print("未检测到摄像头设备")
            return None

        camera_devices.sort(key=lambda x: int(re.findall(r'\d+', x)[0]), reverse=True)

        for dev_path in camera_devices:
            try:
                cap = cv2.VideoCapture(dev_path)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                cap.set(cv2.CAP_PROP_FPS, 30)

                if cap.isOpened():
                    ret, _ = cap.read()
                    if ret:
                        print(f"成功打开外接摄像头: {dev_path}")
                        return cap
                    else:
                        cap.release()
            except Exception as e:
                print(f"尝试打开{dev_path}失败: {e}")

        print("所有摄像头设备均无法打开")
        return None

    def _get_latest_frame(self):
        latest_frame = None
        temp_frames = []
        while not self.video_queue.empty():
            temp_frames.append(self.video_queue.get())

        if temp_frames:
            latest_frame = temp_frames[-1][0]
            for frame, ts in temp_frames:
                self.video_queue.put((frame, ts))

        return latest_frame

    def initialize_system(self):
        try:
            if not self.initialize_tts():
                return False
            if not self.initialize_models():
                return False
            self.pyaudio_instance = pyaudio.PyAudio()
            self.audio_stream = self.pyaudio_instance.open(
                format=self.FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                frames_per_buffer=AUDIO_CHUNK
            )
            self.is_running = True
            return True
        except Exception as e:
            print(f"系统初始化失败: {e}")
            return False

    def start_listening(self):
        if not self.is_running:
            return

        self.speak(self.welcome_text)
        time.sleep(2)
        self.is_listening = True

        self.speak(self.listening_text)
        print("\n系统已准备就绪，请开始说话...")

        threading.Thread(target=self.audio_recorder, daemon=True).start()
        threading.Thread(target=self.video_recorder, daemon=True).start()
        threading.Thread(target=self._process_commands, daemon=True).start()

    def audio_recorder(self):
        self.last_active_time = time.time()
        audio_buffer = []

        while self.is_listening and self.is_running:
            if self.audio_block_lock.locked():
                time.sleep(0.01)
                continue

            data = self.audio_stream.read(AUDIO_CHUNK, exception_on_overflow=False)
            audio_buffer.append(data)

            if len(audio_buffer) * AUDIO_CHUNK / AUDIO_RATE >= 0.5:
                raw_audio = b''.join(audio_buffer)
                if self.check_vad_activity(raw_audio):
                    print("检测到语音活动")
                    self.last_active_time = time.time()
                    self.segments_to_save.append((raw_audio, time.time()))
                else:
                    print("静音中...")
                audio_buffer = []

            if time.time() - self.last_active_time > NO_SPEECH_THRESHOLD:
                if self.segments_to_save and self.segments_to_save[-1][1] > self.last_vad_end_time:
                    self.process_audio_in_memory()
                    self.last_active_time = time.time()

    def process_audio_in_memory(self):
        if not self.segments_to_save:
            return

        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()

        start_time = self.segments_to_save[0][1]
        end_time = self.segments_to_save[-1][1]
        video_frames = []
        while not self.video_queue.empty():
            frame, timestamp = self.video_queue.get()
            if start_time <= timestamp <= end_time:
                video_frames.append(frame)

        audio_data = b''.join([seg[0] for seg in self.segments_to_save])
        audio_np = np.frombuffer(audio_data, dtype=np.int16)

        audio_16k = resample_poly(audio_np, 16000, AUDIO_RATE)
        audio_float32 = audio_16k.astype(np.float32) / 32768.0

        self.last_nav_query = ""
        try:
            result = self.sensevoice_pipeline(audio_data, audio_fs=16000)
            raw_text = result[0].get("text", "").strip() if isinstance(result, list) else result.get("text", "")
            self.last_nav_query = re.sub(r'<\|.*?\|>|[^\u4e00-\u9fa5，。？！\s]', '', raw_text)
        except:
            pass

        threading.Thread(target=self.inference, args=(video_frames, audio_float32)).start()

        self.saved_intervals.append((start_time, end_time))
        self.segments_to_save.clear()

    def video_recorder(self):
        cap = self._get_camera_capture()
        if not cap:
            self.speak("摄像头初始化失败，请检查硬件连接")
            return

        print("视频录制已开始（外接摄像头）")
        while self.is_listening and self.is_running:
            ret, frame = cap.read()
            if ret:
                if not self.video_queue.full():
                    self.video_queue.put((frame, time.time()), block=False)
                self.video_buffer.append((frame, time.time()))
                cv2.imshow("USB Camera Feed", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
            else:
                print("无法获取摄像头画面，重试中...")
                time.sleep(0.5)

        cap.release()
        cv2.destroyAllWindows()
        print("视频录制已停止")

    def check_vad_activity(self, audio_data):
        THRESHOLD_RATIO = 0.5
        MIN_ENERGY = 10
        STEP_MS = 0.02
        step = int(AUDIO_RATE * STEP_MS)

        total_samples = len(audio_data)
        total_chunks = total_samples // step
        if total_chunks == 0:
            return False

        speech_blocks = 0
        for i in range(total_chunks):
            start = i * step
            end = start + step
            chunk = audio_data[start:end]
            if len(chunk) != step:
                continue
            if self.vad.is_speech(chunk, AUDIO_RATE):
                speech_blocks += 1

        audio_np = np.frombuffer(audio_data, dtype=np.int16)
        energy = np.sqrt(np.mean(audio_np ** 2))
        return speech_blocks > int(total_chunks * THRESHOLD_RATIO) and energy > MIN_ENERGY

    def inference(self, video_frames, audio_data):
        key_frames = []
        if video_frames:
            total_frames = len(video_frames)
            frame_indices = [
                int(total_frames * 0.25),
                int(total_frames * 0.5),
                int(total_frames * 0.75)
            ]
            for idx in frame_indices:
                if 0 <= idx < total_frames:
                    frame = video_frames[idx]
                    pil_frame = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    key_frames.append(pil_frame)

        try:
            result = self.sensevoice_pipeline(audio_data, audio_fs=16000)
            raw_text = result[0].get("text", "").strip() if isinstance(result, list) else result.get("text", "")
            raw_text = re.sub(r'<\|.*?\|>|[^\u4e00-\u9fa5，。？！\s]', '', raw_text)

            if not raw_text or len(raw_text) < 2:
                print(f"无效语音输入: {raw_text}")
                return

            print(f"语音识别结果: {raw_text}")
            self.last_nav_query = raw_text

        except Exception as e:
            print(f"语音识别错误: {e}")
            self.speak("语音识别失败，请重试")
            return

        try:
            prompt = (
                f"请分析以下语音指令，完成两项任务并严格按JSON格式输出：\n"
                f"语音指令：'{raw_text}'\n"
                f"任务1：指令分类\n"
                f" - 分类类型：导航、图像识别、对话、添加任务点、修改任务点、问候\n"
                f" - 分类规则：\n"
                f"   - 含'去'、'到'、'导航'、'拿'、'取'、'找'→导航；\n"
                f"   - 含'识别'、'看'、'检测'→图像识别；\n"
                f"   - 含'添加'、'增加'、'保存'、'记录'→添加任务点；\n"
                f"   - 含'修改'、'设定为'→修改任务点；\n"
                f"   - 含'问候'、'招呼'、'问好'→问候；\n"
                f"   - 无上述关键词→对话\n"
                f"   - 需提取分类依据的关键词（从原始语音中提取）\n"

                f"任务2：语义解析\n"
                f" - 提取target：用户想找/拿/取的核心物品（无则为空）；\n"
                f" - 判断need_navigate：是否需要导航（true/false）；\n"
                f" - 说明reason：分类及导航判断的综合依据\n"
                f"输出格式（仅JSON，无多余文本）：\n"
                f'{{"type": "指令类型", "keyword": "分类关键词", "target": "目标物品", "need_navigate": true/false, "reason": "综合判断依据"}}\n'
                f"示例：\n"
                f'指令："去拿桌子上的水瓶" → 输出：{{"type": "导航", "keyword": "去、拿", "target": "水瓶", "need_navigate": true, "reason": "含\'去、拿\'关键词属于导航，需拿水瓶故需要导航"}}\n'
                f'指令："识别一下这是什么" → 输出：{{"type": "图像识别", "keyword": "识别", "target": "", "need_navigate": false, "reason": "含\'识别\'关键词属于图像识别，与导航无关"}}\n'
                f'指令："今天天气如何" → 输出：{{"type": "对话", "keyword": "", "target": "", "need_navigate": false, "reason": "无特定关键词属于对话，与导航无关"}}'
            )

            messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            if key_frames:
                for i, frame in enumerate(key_frames):
                    messages[0]["content"].insert(i, {"type": "image", "image": frame})

            text = self.qwen_processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            image_inputs, _ = process_vision_info(messages) if key_frames else (None, None)

            inputs = self.qwen_processor(
                text=[text],
                images=image_inputs,
                padding=True,
                return_tensors="pt"
            ).to(self.device)

            generated_ids = self.qwen_model.generate(**inputs, max_new_tokens=200)
            generated_ids_trimmed = generated_ids[0][len(inputs['input_ids'][0]):]
            combined_result = self.qwen_processor.decode(generated_ids_trimmed, skip_special_tokens=True).strip()

            print(f"融合解析结果: {combined_result}")
            self.process_combined_result(combined_result, raw_text)

        except Exception as e:
            print(f"指令解析错误: {e}")
            self.speak("未能理解您的指令，请重新表述")

    def process_combined_result(self, combined_result, raw_text):
        cleaned_result = re.sub(r'^```json\s*', '', combined_result)
        cleaned_result = re.sub(r'\s*```$', '', cleaned_result)
        cleaned_result = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\{\}\"\:\,\s\_truefalse]', '', cleaned_result).strip()
        print(f"清理后的融合结果: {cleaned_result}")

        try:
            result = json.loads(cleaned_result)

            cmd_type = result.get("type", "").strip()
            keyword = result.get("keyword", "").strip()
            target = result.get("target", "").strip()
            need_navigate = result.get("need_navigate", False)
            reason = result.get("reason", "无说明")

            valid_types = ["导航", "图像识别", "对话", "添加任务点", "修改任务点", "问候"]
            if cmd_type not in valid_types:
                print(f"检测到无效指令类型: {cmd_type}，尝试自动推断...")

                if any(kw in raw_text for kw in ["去", "到", "导航", "拿", "取", "找"]):
                    cmd_type = "导航"
                elif any(kw in raw_text for kw in ["识别", "看", "检测"]):
                    cmd_type = "图像识别"
                elif any(kw in raw_text for kw in ["添加", "增加", "保存", "记录"]):
                    cmd_type = "添加任务点"
                elif any(kw in raw_text for kw in ["修改", "设定为"]):
                    cmd_type = "修改任务点"
                elif any(kw in raw_text for kw in ["问候", "招呼", "问好"]):
                    cmd_type = "问候"
                else:
                    cmd_type = "对话"

                print(f"自动推断指令类型为: {cmd_type}")

            print(f"融合解析详情：类型={cmd_type}，关键词={keyword}，目标={target}，需导航={need_navigate}，依据={reason}")

        except (json.JSONDecodeError, ValueError) as e:
            print(f"融合结果解析错误: {e}，原始输出: {combined_result}")
            print("尝试直接根据原始文本推断指令类型...")
            if any(kw in raw_text for kw in ["去", "到", "导航", "拿", "取", "找"]):
                cmd_type = "导航"
                target = raw_text
                need_navigate = True
            elif any(kw in raw_text for kw in ["识别", "看", "检测"]):
                cmd_type = "图像识别"
                target = ""
                need_navigate = False
            elif any(kw in raw_text for kw in ["添加", "增加", "保存", "记录"]):
                cmd_type = "添加任务点"
                target = ""
                need_navigate = False
            elif any(kw in raw_text for kw in ["修改", "设定为"]):
                cmd_type = "修改任务点"
                target = ""
                need_navigate = False
            elif any(kw in raw_text for kw in ["问候", "招呼", "问好"]):
                cmd_type = "问候"
                target = ""
                need_navigate = False
            else:
                cmd_type = "对话"
                target = raw_text
                need_navigate = False

            print(f"从原始文本推断：类型={cmd_type}，目标={target}")
            keyword = ""
            reason = "解析失败后从原始文本推断"

        image_keywords = {"检测", "识别", "图像", "目标检测", "摄像头", "看", "分析"}
        has_image_kw = any(kw in raw_text for kw in image_keywords)
        if has_image_kw and cmd_type != "图像识别":
            print(f"强制修正类型：{cmd_type} → 图像识别（原始语音含图像关键词）")
            cmd_type = "图像识别"

        for kw, cmd in self.command_mapping.items():
            if kw in raw_text:
                print(f"触发关键词直接匹配：{kw} → {cmd}")
                self.command_queue.put((cmd, kw))
                return

        try:
            if cmd_type == "导航":
                if need_navigate and target:
                    print(f"尝试用目标物品'{target}'匹配任务点...")
                    matched_point = self.find_point_by_environment(target)
                    if matched_point:
                        print(f"找到匹配任务点: {matched_point}，直接导航")
                        desc = self.task_points.get(matched_point, {}).get("environment", "未知位置")
                        self.speak(f"正在导航到{target}所在位置（任务点{matched_point}，{desc}）")
                        self.send_goal_point(matched_point)
                        return
                    else:
                        self.speak(f"未找到包含'{target}'的任务点，将按原始指令搜索")

                self.command_queue.put((CommandType.NAVIGATE, raw_text))

            elif cmd_type == "图像识别":
                self.command_queue.put((CommandType.IMAGE_RECOGNITION, keyword or raw_text))

            elif cmd_type == "对话":
                dialogue_content = f"{target}相关内容：{raw_text}" if target else raw_text
                self.command_queue.put((CommandType.DIALOGUE, dialogue_content))

            elif cmd_type == "添加任务点":
                point_desc = target if target else keyword
                self.command_queue.put((CommandType.ADD_TASK_POINT, point_desc))

            elif cmd_type == "修改任务点":
                point_id = result.get("point_id", 0) if isinstance(result, dict) else 0
                new_desc = target if target else keyword
                self.command_queue.put(((CommandType.MODIFY_TASK_POINT, point_id), new_desc))

            elif cmd_type == "问候":
                self.command_queue.put((CommandType.GREET, 9))

        except Exception as e:
            print(f"融合结果处理逻辑错误: {e}")
            self.speak("处理指令时出错，请重试")

    def _process_commands(self):
        while self.is_running:
            try:
                command = self.command_queue.get(timeout=1)
                cmd_code, param = command[0], command[1]
                if isinstance(cmd_code, tuple):
                    cmd_type, sub_param = cmd_code
                    print(f"解析到元组指令：类型={cmd_type}，子参数={sub_param}，参数={param}")
                else:
                    cmd_type = cmd_code
                    sub_param = None
                    print(f"解析到普通指令：类型={cmd_type}，参数={param}")

                self.speak(f"收到指令: {param}")

                if cmd_type == CommandType.ADD_TASK_POINT:
                    self.add_task_point()
                elif cmd_type == CommandType.MODIFY_TASK_POINT:
                    print(f"准备调用modify_task_point，参数point_id={sub_param}")
                    self.modify_task_point(sub_param)
                    print(f"modify_task_point调用完成，参数point_id={sub_param}")
                elif cmd_type == CommandType.IMAGE_RECOGNITION:
                    self.process_image_recognition()
                elif cmd_type == CommandType.DIALOGUE:
                    self.process_dialogue(param)
                elif cmd_type == CommandType.GREET:
                    self.send_lerobot(int(sub_param))
                elif cmd_type == CommandType.NAVIGATE:
                    self.handle_navigation(command_code=sub_param, query_text=param)
                else:
                    print(f"未知指令类型: {cmd_type}")
                    self.speak("未知指令，请重试")

                self.command_queue.task_done()
            except Empty:
                continue
            except Exception as e:
                print(f"命令处理错误: {e}")
                self.speak("处理指令时出错，请重试")

    def stop_system(self):
        print("开始关闭系统...")
        self.is_listening = False
        self.is_running = False
        self.recording_active = False

        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()

        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

        if self.audio_stream:
            self.audio_stream.stop_stream()
            self.audio_stream.close()

        if self.pyaudio_instance:
            self.pyaudio_instance.terminate()

        if hasattr(self, 'audio_thread') and self.audio_thread.is_alive():
            self.audio_thread.join(timeout=2)

        if hasattr(self, 'video_thread') and self.video_thread.is_alive():
            self.video_thread.join(timeout=2)

        if hasattr(self, 'command_thread') and self.command_thread.is_alive():
            self.command_thread.join(timeout=2)

        if self.device and self.device.type == 'cuda':
            torch.cuda.empty_cache()

        print("系统已完全关闭")

    def run(self):
        try:
            self.start_listening()
            while self.is_running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.speak("收到退出信号，再见")
            time.sleep(1)
        finally:
            self.stop_system()


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


# ===== ROS2 Node Wrapper =====
class VoiceModuleNode(Node):
    def __init__(self):
        super().__init__('voice_module_node')

        self.declare_parameter('mqtt_broker', '10.42.0.1')
        self.declare_parameter('mqtt_port', 1883)
        self.declare_parameter('qwen_model_path', '/home/y/LLM/Qwen2-VL-2B-Instruct')
        self.declare_parameter('sensevoice_model_path', '/home/y/LLM/SenseVoiceSmall')

        self.goal_pub = self.create_publisher(PoseStamped, '/voice_nav_goal', 10)

        self.voice_system = None
        self.voice_thread = None
        self.get_logger().info('VoiceModuleNode 正在初始化语音系统...')

        self.init_timer = self.create_timer(0.5, self.init_voice_system)

    def init_voice_system(self):
        if hasattr(self, 'init_timer'):
            self.init_timer.cancel()
        self.get_logger().info('正在启动语音交互系统...')
        try:
            mqtt_broker = self.get_parameter('mqtt_broker').value
            mqtt_port = self.get_parameter('mqtt_port').value

            self.voice_system = VoiceNavigationSystem(
                goal_publisher=self.goal_pub,
                node=self
            )
            self.voice_system.mqtt_broker = mqtt_broker
            self.voice_system.mqtt_port = mqtt_port

            def _run_voice():
                try:
                    self.voice_system.run()
                except Exception as e:
                    self.get_logger().error(f'语音系统运行异常: {e}')

            self.voice_thread = threading.Thread(target=_run_voice, daemon=True)
            self.voice_thread.start()
            self.get_logger().info('语音交互系统已启动')
        except Exception as e:
            self.get_logger().error(f'语音系统初始化失败: {e}')
            import traceback
            self.get_logger().error(traceback.format_exc())

    def destroy_node(self):
        self.get_logger().info('正在关闭语音交互系统...')
        if self.voice_system:
            self.voice_system.stop_system()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = VoiceModuleNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
