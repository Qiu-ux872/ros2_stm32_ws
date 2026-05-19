import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory('asr_bridge')

    default_cmd_map = os.path.join(pkg_share, 'config', 'commands.yaml')

    return LaunchDescription([

        DeclareLaunchArgument(
            'asr_port',
            default_value='/dev/ttyUSB2',
            description='ASR-PRO serial port'
        ),
        DeclareLaunchArgument(
            'asr_baud',
            default_value='9600',
            description='ASR-PRO baud rate'
        ),
        DeclareLaunchArgument(
            'task_points_file',
            default_value='',
            description='Path to task_points.json (auto-detect if empty)'
        ),
        DeclareLaunchArgument(
            'command_map_file',
            default_value=default_cmd_map,
            description='Path to command map YAML'
        ),

        Node(
            package='asr_bridge',
            executable='asr_bridge',
            name='asr_bridge_node',
            output='screen',
            parameters=[{
                'serial_port': LaunchConfiguration('asr_port'),
                'baud_rate': LaunchConfiguration('asr_baud'),
                'task_points_file': LaunchConfiguration('task_points_file'),
                'command_map_file': LaunchConfiguration('command_map_file'),
            }]
        ),
    ])
