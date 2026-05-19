import sys
if sys.prefix == '/usr':
    sys.real_prefix = sys.prefix
    sys.prefix = sys.exec_prefix = '/home/qiu/Desktop/ros2_stm32_ws/install/voice_bridge'
