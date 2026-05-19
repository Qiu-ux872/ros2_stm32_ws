from setuptools import find_packages, setup
import os
from glob import glob

package_name = 'asr_bridge'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'config'),
            glob('config/*.yaml')),
        (os.path.join('share', package_name, 'launch'),
            glob('launch/*.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='qiu',
    maintainer_email='nianan_0311@qq.com',
    description='ASR-PRO offline voice module bridge for ROS2 navigation',
    license='TODO: License declaration',
    extras_require={
        'test': ['pytest'],
    },
    entry_points={
        'console_scripts': [
            'asr_bridge = asr_bridge.asr_bridge_node:main',
        ],
    },
)
